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
  AppStore._(this._prefs, this.baseUrl, this.targets,
      this.relayUrl, this.relayToken);

  static const _keyBaseUrl = 'base_url';
  static const _keyTargets = 'wake_targets';
  static const _keyRelayUrl = 'relay_url';
  static const _keyRelayToken = 'relay_token';

  final SharedPreferences _prefs;
  String baseUrl;
  List<WakeTarget> targets;

  /// 「中转设备」的控制台地址：手机不在电脑所在网段时，改请它代发魔术包。
  ///
  /// 空串表示没配——这时只走同网段直发那条路，行为跟以前一样。
  String relayUrl;

  /// 中转设备的 API 令牌，由用户从那边控制台抄来。
  ///
  /// 明文存在 SharedPreferences 里。这是妥协不是设计：手机与那台设备之间的
  /// 通道本来就可能是明文 HTTP，令牌也只在校园网内有效；真要防，得先给后端
  /// 上 TLS。别因为这条就去引进 flutter_secure_storage 之类的新依赖。
  String relayToken;

  /// 中转是否已配置齐（地址与令牌都要有，缺一不可）。
  bool get relayReady => relayUrl.isNotEmpty && relayToken.isNotEmpty;

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
    return AppStore._(
      prefs,
      prefs.getString(_keyBaseUrl) ?? '',
      targets,
      prefs.getString(_keyRelayUrl) ?? '',
      prefs.getString(_keyRelayToken) ?? '',
    );
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

  /// 存中转配置。传空串就是关掉中转。
  Future<void> saveRelay({required String url, required String token}) async {
    relayUrl = url;
    relayToken = token;
    if (url.isEmpty) {
      // 没配地址却留着令牌等于留了一份用不上的凭据，一起清掉。
      await _prefs.remove(_keyRelayUrl);
      await _prefs.remove(_keyRelayToken);
      return;
    }
    await _prefs.setString(_keyRelayUrl, url);
    await _prefs.setString(_keyRelayToken, token);
  }

  Future<void> clear() async {
    baseUrl = '';
    targets = const [];
    relayUrl = '';
    relayToken = '';
    await _prefs.remove(_keyBaseUrl);
    await _prefs.remove(_keyTargets);
    await _prefs.remove(_keyRelayUrl);
    await _prefs.remove(_keyRelayToken);
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
  // Uri.host 会把 IPv6 字面量的方括号去掉（`[::1]` → `::1`），直接拼回去
  // 得到的是 `http://::1:2268`，既不是合法 URL，也连不上。
  final host = uri.host;
  final authority = host.contains(':') ? '[$host]' : host;
  final buffer = StringBuffer('${uri.scheme}://$authority:$port');
  if (uri.path.isNotEmpty && uri.path != '/') {
    buffer.write(uri.path);
  }
  return buffer.toString();
}
