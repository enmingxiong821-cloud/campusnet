/// 本地持久化：控制台地址，以及「关机状态下也能唤醒」所需的那点信息。
///
/// 为什么非存本地不可：唤醒必须能在**电脑关着**的时候用，那时后端连不上，
/// 什么也问不到。所以网卡 MAC 与广播地址要在电脑还开着的时候抓一次存下来。
library;

import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// 一台可唤醒的设备。字段与后端 `/api/power/info`、`/api/devices` 的返回对齐。
class WakeTarget {
  const WakeTarget({
    required this.name,
    required this.mac,
    this.broadcast = '',
  });

  final String name;
  final String mac;
  final String broadcast;

  Map<String, dynamic> toJson() => {
        'name': name,
        'mac': mac,
        'broadcast': broadcast,
      };

  static WakeTarget? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final mac = '${raw['mac'] ?? ''}'.trim();
    if (mac.isEmpty) return null;
    return WakeTarget(
      name: '${raw['name'] ?? ''}'.trim(),
      mac: mac,
      broadcast: '${raw['broadcast'] ?? ''}'.trim(),
    );
  }
}

class AppStore {
  AppStore._(this._prefs, this.baseUrl, this.targets);

  static const _keyBaseUrl = 'base_url';
  static const _keyTargets = 'wake_targets';

  final SharedPreferences _prefs;
  String baseUrl;
  List<WakeTarget> targets;

  static Future<AppStore> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_keyTargets);
    final targets = <WakeTarget>[];
    if (raw != null && raw.isNotEmpty) {
      try {
        final decoded = jsonDecode(raw);
        if (decoded is List) {
          for (final item in decoded) {
            final target = WakeTarget.fromJson(item);
            if (target != null) targets.add(target);
          }
        }
      } on FormatException {
        // 存坏了就当没有。这里不值得让 APP 起不来——用户重填一次即可。
      }
    }
    return AppStore._(prefs, prefs.getString(_keyBaseUrl) ?? '', targets);
  }

  Future<void> saveBaseUrl(String value) async {
    baseUrl = value;
    await _prefs.setString(_keyBaseUrl, value);
  }

  Future<void> saveTargets(List<WakeTarget> value) async {
    targets = List.unmodifiable(value);
    await _prefs.setString(
      _keyTargets,
      jsonEncode([for (final target in value) target.toJson()]),
    );
  }

  Future<void> clear() async {
    baseUrl = '';
    targets = const [];
    await _prefs.remove(_keyBaseUrl);
    await _prefs.remove(_keyTargets);
  }
}

/// 把用户随手敲的地址收拾成能用的形式：补上 scheme、去掉末尾斜杠。
///
/// 允许只填 IP 或主机名——用户从控制台抄来的往往是「192.168.1.5:2268」
/// 这种，不带 http://。端口留空时补 2268（后端默认端口）。
/// 收拾完仍然不像地址就抛 [FormatException]，由调用方提示用户。
String normalizeBaseUrl(String input, {int defaultPort = 2268}) {
  var text = input.trim();
  if (text.isEmpty) {
    throw const FormatException('地址不能为空');
  }
  if (!text.contains('://')) {
    text = 'http://$text';
  }
  final uri = Uri.tryParse(text);
  if (uri == null || uri.host.isEmpty) {
    throw FormatException('看不懂这个地址：$input');
  }
  // `Uri` 遇到主机名里的非法字符不报错，而是百分号转义后照收——于是
  // 「不是地址」这种手误会被存成 `http://%E4%B8%8D...:2268`，一个谁也连不上
  // 的地址，用户只会看到「连不上」而想不到是自己打错了。转义是非法字符留下的
  // 唯一痕迹，据此拦掉。（IPv6 字面量的方括号不含 %，不受影响。）
  if (uri.host.contains('%')) {
    throw FormatException('看不懂这个地址：$input');
  }
  if (uri.scheme != 'http' && uri.scheme != 'https') {
    throw FormatException('只支持 http/https，收到的是 ${uri.scheme}');
  }
  final port = uri.hasPort ? uri.port : defaultPort;
  final buffer = StringBuffer('${uri.scheme}://${uri.host}:$port');
  if (uri.path.isNotEmpty && uri.path != '/') {
    buffer.write(uri.path);
  }
  return buffer.toString();
}
