/// 与后端打交道的那一小部分。
///
/// 除下面明说的那一处例外，这里只用免鉴权接口：带鉴权的（读网卡 MAC、改设置、
/// 关机）都交给 WebView 里的控制台去做——它自己带着会话 Cookie，APP 不必去碰
/// 鉴权这套东西。
///
/// 例外是 [relayWake]。它要打的是**另一台**电脑（宿舍里那台常开的小设备），
/// 手机上没有它的会话，也借不到——那台机器上并没有页面在跑。所以这条路显式
/// 带着用户自己粘进来的令牌，且令牌只存本机。除它之外不要在这里加带鉴权的调用。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'wol.dart' show macKey;

/// 后端探活。
///
/// `/api/health` 在免鉴权白名单里（`server.py:1230`），所以电脑刚开机、还没配对、
/// 会话过期都无所谓，它照样答。APP 靠它判断该显示控制台还是「电脑关机了」。
///
/// 探活失败不是异常——「连不上」正是这个函数要回答的问题之一。
Future<bool> ping(
  String baseUrl, {
  Duration timeout = const Duration(seconds: 4),
}) async {
  final client = HttpClient()..connectionTimeout = timeout;
  try {
    final request = await client
        .getUrl(Uri.parse('$baseUrl/api/health'))
        .timeout(timeout);
    final response = await request.close().timeout(timeout);
    await response.drain<void>();
    return response.statusCode == 200;
  } on SocketException {
    return false; // 连不上（关机、不在同一网段、地址写错）
  } on TimeoutException {
    return false; // 丢包挂起——校园网常见，按「不通」处理
  } on HttpException {
    return false;
  } on FormatException {
    return false; // 地址本身不合法
  } finally {
    client.close(force: true);
  }
}

/// 请中转设备代发唤醒包的结果。
///
/// 不用 bool：这几种失败该说的话完全不同——令牌错要重抄一遍令牌，设备表里没有
/// 要先去那台机器上登记，连不上要查地址。混成一句「唤醒失败」等于没说。
enum RelayOutcome {
  /// 中转设备已经把魔术包发出去了。
  ok,

  /// 那台设备的控制台上没登记这块网卡（它按设备 id 发，不认裸 MAC）。
  notFound,

  /// 令牌不对或没填。
  unauthorized,

  /// 连不上中转设备本身。
  unreachable,

  /// 连上了但没发成功。
  failed,
}

/// 请中转设备替我们发一次唤醒包。
///
/// 手机不在电脑所在的那个二层网络里时（人在外面用流量），广播出不去；宿舍里那台
/// 常开的小设备在网内，让它发就行。走的是后端现成的 `/api/devices` 与
/// `/api/devices/{id}/wake`，所以**那块网卡得先在那台设备的控制台上登记过**——
/// 没有就返回 [RelayOutcome.notFound]。
Future<RelayOutcome> relayWake(
  String relayUrl,
  String token,
  String mac, {
  Duration timeout = const Duration(seconds: 6),
}) async {
  final target = relayUrl.trim();
  if (target.isEmpty) return RelayOutcome.unreachable;
  final client = HttpClient()..connectionTimeout = timeout;
  try {
    final listed = await _call(client, 'GET', '$target/api/devices', token, timeout);
    if (listed == null) return RelayOutcome.unreachable;
    if (listed.status == HttpStatus.unauthorized ||
        listed.status == HttpStatus.forbidden) {
      return RelayOutcome.unauthorized;
    }
    if (listed.status != HttpStatus.ok) return RelayOutcome.failed;

    final id = pickDeviceId(listed.json, mac);
    if (id == null) return RelayOutcome.notFound;

    final woken = await _call(
      client,
      'POST',
      '$target/api/devices/$id/wake',
      token,
      timeout,
    );
    if (woken == null) return RelayOutcome.unreachable;
    if (woken.status == HttpStatus.unauthorized ||
        woken.status == HttpStatus.forbidden) {
      return RelayOutcome.unauthorized;
    }
    return woken.status == HttpStatus.ok ? RelayOutcome.ok : RelayOutcome.failed;
  } finally {
    client.close(force: true);
  }
}

/// 从 `/api/devices` 的返回里挑出这块网卡对应的设备 id；没有就返回 null。
///
/// 单拎出来是因为真正联网的那段没法在单测里跑，而这个「按 MAC 找 id」的判断能，
/// 而且它正是最容易出错的地方（大小写、冒号、设备表里混着别的网卡）。
int? pickDeviceId(Object? payload, String mac) {
  if (payload is! Map) return null;
  final devices = payload['devices'];
  if (devices is! List) return null;
  final wanted = macKey(mac);
  if (wanted.isEmpty) return null;
  for (final device in devices) {
    if (device is! Map) continue;
    if (macKey('${device['mac'] ?? ''}') != wanted) continue;
    final id = device['id'];
    if (id is int) return id;
    final parsed = int.tryParse('$id');
    if (parsed != null) return parsed;
  }
  return null;
}

/// 发一次带令牌的请求，返回状态码与解析好的 JSON；发不出去返回 null。
///
/// 「发不出去」（连不上、超时、地址不合法）与「发出去了但被拒」在这里就分开：
/// 前者 null，后者照样带状态码回来，调用方才能区分「地址写错」和「令牌过期」。
Future<({int status, Object? json})?> _call(
  HttpClient client,
  String method,
  String url,
  String token,
  Duration timeout,
) async {
  try {
    final request = await client
        .openUrl(method, Uri.parse(url))
        .timeout(timeout);
    // auth.py 的 _token_from_headers 两个头都认；这个不用管 Bearer 前缀。
    request.headers.set('X-CampusNet-Token', token);
    // 唤醒接口不吃请求体，所以显式声明长度 0。**别改成写一个 '{}' 进来**：
    // HttpClient 在不设 contentLength 又写了内容时会改用 chunked 传输，
    // 而后端明确拒绝 Transfer-Encoding（server.py 的 _read_raw_body 回 400
    // 并断开连接），客户端这边只会看到一句「Connection closed while receiving
    // data」，长得像「中转设备连不上」，排查时方向全错。
    request.contentLength = 0;
    final response = await request.close().timeout(timeout);
    final text = await response.transform(utf8.decoder).join().timeout(timeout);
    Object? json;
    try {
      json = jsonDecode(text);
    } on FormatException {
      json = null; // 不是 JSON 也照样把状态码交回去
    }
    return (status: response.statusCode, json: json);
  } on IOException {
    return null; // SocketException / HttpException / TLS 握手失败
  } on TimeoutException {
    return null;
  } on FormatException {
    return null; // 地址本身不合法
  }
}
