/// WOL（网络唤醒）的纯 Dart 实现。
///
/// 这是整个 APP 存在的主要理由：电脑关着的时候后端进程没在跑，任何网页
/// 都够不着它；只有能直接往局域网发广播的原生代码才叫得醒它。
///
/// 格式与后端 `project/backend/campusnet/power.py` 的 `magic_packet()` 同构：
/// 6 个 0xFF，紧跟目标网卡 MAC 重复 16 次，共 102 字节。两边必须逐字节一致，
/// 所以 `test/wol_test.dart` 用固定向量把这份钉住了——改这里就要回头对后端。
library;

import 'dart:io';
import 'dart:typed_data';

/// 与后端 `power.py` 的 `WOL_PORT` 保持一致。9 是 WOL 的惯用端口，
/// 7 也能用，但绝大多数网卡默认只听 9。
const int wolPort = 9;

/// 兜底的受限广播地址。部分 Android 版本会拦它，所以真正的首选是
/// 子网定向广播（如 192.168.1.255），见 [guessBroadcast]。
const String globalBroadcast = '255.255.255.255';

class WolError implements Exception {
  WolError(this.message);

  final String message;

  @override
  String toString() => message;
}

/// 把各种写法的 MAC 归一成 12 位大写十六进制。
///
/// 宽松度刻意对齐后端的 `re.sub(r"[^0-9a-fA-F]", "", mac)`：冒号、短横线、
/// 点、空格一律忽略。后端只校验最终长度是 12，这里也一样。
String normalizeMac(String mac) {
  final cleaned = mac.replaceAll(RegExp('[^0-9a-fA-F]'), '');
  if (cleaned.length != 12) {
    throw WolError('MAC 地址格式不对：$mac（应形如 AA:BB:CC:DD:EE:FF）');
  }
  return cleaned.toUpperCase();
}

/// 组装魔术包。长度固定 102 字节。
Uint8List magicPacket(String mac) {
  final hex = normalizeMac(mac);
  final packet = Uint8List(6 + 16 * 6);
  packet.fillRange(0, 6, 0xFF);
  for (var repeat = 0; repeat < 16; repeat++) {
    for (var byte = 0; byte < 6; byte++) {
      final start = byte * 2;
      packet[6 + repeat * 6 + byte] =
          int.parse(hex.substring(start, start + 2), radix: 16);
    }
  }
  return packet;
}

/// 从控制台地址的 host 猜一个子网定向广播地址（按 /24 算）。
///
/// 猜错无所谓：发送时会同时打全局广播，两个里通一个就行。之所以要猜，
/// 是因为 Android 各版本对 255.255.255.255 的处理不一致，定向广播更稳。
/// 不是点分 IPv4（比如 Tailscale 的域名）就返回 null。
String? guessBroadcast(String host) {
  final parts = host.split('.');
  if (parts.length != 4) return null;
  for (final part in parts) {
    final value = int.tryParse(part);
    if (value == null || value < 0 || value > 255) return null;
  }
  return '${parts[0]}.${parts[1]}.${parts[2]}.255';
}

/// 挑出该往哪个定向广播地址发包。
///
/// 优先用后端给的值——将来后端补上真正的子网定向广播（现在 `wake_info()`
/// 写死全局广播）时，这里自动受益。后端只回全局广播时那个值没有增量信息，
/// 改用按控制台地址猜的那个：控制台地址就是那台电脑，跟它同网段。
/// 两边都给不出就返回空串，调用方只发全局广播。
String directedBroadcastFor(String host, String fromBackend) {
  final value = fromBackend.trim();
  if (value.isNotEmpty && value != globalBroadcast) return value;
  return guessBroadcast(host) ?? '';
}

/// 把归一化后的 MAC 还原成 `AA:BB:CC:DD:EE:FF`。纯粹为了给人看——
/// 12 位连排的十六进制没人能一眼对上贴在机箱上的那串。
String formatMac(String mac) {
  final hex = normalizeMac(mac);
  final parts = <String>[];
  for (var index = 0; index < hex.length; index += 2) {
    parts.add(hex.substring(index, index + 2));
  }
  return parts.join(':');
}

/// 往一个广播地址发一次魔术包，返回实际发出的字节数。
///
/// 每次都用新 socket：绑定时会占用一个随机端口，随用随关最省心，也避免
/// 上一次的 socket 还在收包时把这次的状态搅乱。
Future<int> _sendOnce(Uint8List packet, String broadcast, int port) async {
  final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
  try {
    // 不发这一句，Android/Linux 会把发往广播地址的包直接拒掉。
    socket.broadcastEnabled = true;
    return socket.send(packet, InternetAddress(broadcast), port);
  } on SocketException catch (error) {
    throw WolError('往 $broadcast 发包失败：${error.message}');
  } finally {
    socket.close();
  }
}

/// 唤醒一台设备：把魔术包同时打向定向广播与全局广播。
///
/// 返回真正发成功的广播地址列表。两个都发是刻意的——定向广播在部分路由器上
/// 被丢，全局广播在部分 Android 上被拦，同时打才有兜底；重复发包对网卡无害。
///
/// [broadcast] 传空或省略时只用全局广播。地址重复不会发两遍。
Future<List<String>> wake(
  String mac, {
  String broadcast = '',
  int port = wolPort,
}) async {
  final packet = magicPacket(mac);
  final targets = <String>{
    if (broadcast.trim().isNotEmpty) broadcast.trim(),
    globalBroadcast,
  };
  final succeeded = <String>[];
  final failures = <String>[];
  for (final target in targets) {
    try {
      final sent = await _sendOnce(packet, target, port);
      if (sent == packet.length) {
        succeeded.add(target);
      } else {
        failures.add('$target（只发出 $sent/${packet.length} 字节）');
      }
    } on WolError catch (error) {
      failures.add(error.message);
    }
  }
  if (succeeded.isEmpty) {
    throw WolError('魔术包没能发出去：${failures.join('；')}');
  }
  return succeeded;
}
