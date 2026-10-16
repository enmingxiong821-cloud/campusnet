/// 首次配置页：填控制台地址、可选配对码，然后进控制台。
library;

import 'package:flutter/material.dart';

import 'api.dart';
import 'store.dart';

class SetupPage extends StatefulWidget {
  const SetupPage({super.key, required this.store, required this.onDone});

  final AppStore store;

  /// 保存成功后由外部切到控制台页。带配对码时把码一起交出去，
  /// 由控制台页拼进 URL 的 `#pair=` 段。
  final void Function(String baseUrl, String pairCode) onDone;

  @override
  State<SetupPage> createState() => _SetupPageState();
}

class _SetupPageState extends State<SetupPage> {
  late final TextEditingController _urlController =
      TextEditingController(text: widget.store.baseUrl);
  final TextEditingController _pairController = TextEditingController();
  late final TextEditingController _relayUrlController =
      TextEditingController(text: widget.store.relayUrl);
  late final TextEditingController _relayTokenController =
      TextEditingController(text: widget.store.relayToken);
  bool _checking = false;
  String _error = '';

  @override
  void dispose() {
    _urlController.dispose();
    _pairController.dispose();
    _relayUrlController.dispose();
    _relayTokenController.dispose();
    super.dispose();
  }

  /// 收拾中转那两个框。返回值里 url 空串表示「不用中转」。
  ///
  /// 校验只做「填了地址就必须有令牌」——中转那台电脑一旦监听非回环地址，后端
  /// 就会强制要令牌（`server.py` 的 `auth_required`），只填一半必然连不上，
  /// 与其等用户跑到离线页看一句「拒绝了令牌」，不如在这儿就拦下。
  ({String url, String token}) _readRelay() {
    final rawUrl = _relayUrlController.text.trim();
    if (rawUrl.isEmpty) return (url: '', token: '');
    final token = _relayTokenController.text.trim();
    if (token.isEmpty) {
      throw const FormatException('填了中转地址，就得把它的令牌一起填上');
    }
    return (url: normalizeBaseUrl(rawUrl), token: token);
  }

  Future<void> _connect() async {
    final String baseUrl;
    final ({String url, String token}) relay;
    try {
      baseUrl = normalizeBaseUrl(_urlController.text);
      relay = _readRelay();
    } on FormatException catch (error) {
      setState(() => _error = error.message);
      return;
    }
    setState(() {
      _checking = true;
      _error = '';
    });
    final reachable = await ping(baseUrl);
    if (!mounted) return;
    setState(() => _checking = false);

    if (reachable) {
      await _save(baseUrl, relay);
      return;
    }
    // 连不上也允许保存：配置本来就该能在电脑关机时做完——不然第一台设备
    // 要等到人在电脑跟前才能配。只提醒一句，不拦。
    if (!mounted) return;
    final saveAnyway = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('现在连不上'),
        content: Text(
          '$baseUrl 没有响应。\n\n'
          '如果那台电脑正关着机，这是正常的，地址仍然可以保存；'
          '等它开机后 APP 会自动接上。\n\n'
          '如果电脑开着，请检查：\n'
          '· 服务是否用 --host 0.0.0.0 启动（只监听本机时手机连不上）\n'
          '· Windows 防火墙是否放行了该端口\n'
          '· 手机是否和电脑在同一个 Wi-Fi 下（不在的话，配个中转设备）',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('返回检查'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('仍然保存'),
          ),
        ],
      ),
    );
    if (saveAnyway == true) {
      await _save(baseUrl, relay);
    }
  }

  Future<void> _save(String baseUrl, ({String url, String token}) relay) async {
    await widget.store.saveBaseUrl(baseUrl);
    await widget.store.saveRelay(url: relay.url, token: relay.token);
    if (!mounted) return;
    widget.onDone(baseUrl, _pairController.text.trim());
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('连接控制台')),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          const Text(
            '这个 APP 是控制台的手机外壳。填上电脑上控制台的地址，'
            '之后就能像应用一样一键打开它。',
            style: TextStyle(height: 1.5),
          ),
          const SizedBox(height: 24),
          TextField(
            controller: _urlController,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: '控制台地址',
              hintText: '192.168.1.5:2268',
              helperText: '电脑上跑 `campusnet access` 会打印这个地址',
              border: OutlineInputBorder(),
            ),
            onSubmitted: (_) => _connect(),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _pairController,
            keyboardType: TextInputType.number,
            decoration: const InputDecoration(
              labelText: '配对码（可选）',
              hintText: '6 位数字',
              helperText: '电脑控制台点「生成配对码」拿到。留空则进控制台后再登录',
              border: OutlineInputBorder(),
            ),
            onSubmitted: (_) => _connect(),
          ),
          const SizedBox(height: 24),
          const Divider(),
          const SizedBox(height: 16),
          const Text(
            '中转设备（可选）',
            style: TextStyle(fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 6),
          const Text(
            '手机和电脑不在同一个 Wi-Fi 时，广播出不了你所在的网络，得请一台'
            '常开的小设备（宿舍里那台小主机、树莓派之类）代发唤醒包。'
            '那台设备要跑着同样的服务。',
            style: TextStyle(height: 1.5, fontSize: 13),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _relayUrlController,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: '中转设备地址（留空即不用中转）',
              hintText: '10.110.211.9:2268',
              helperText: '那台设备上跑 `campusnet access` 会打印它的地址',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _relayTokenController,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: '中转设备令牌',
              helperText: '在那台设备上执行 python utils/run.py token 得到',
              border: OutlineInputBorder(),
            ),
          ),
          if (_error.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(
              _error,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
          const SizedBox(height: 24),
          FilledButton(
            onPressed: _checking ? null : _connect,
            child: _checking
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('测试并保存'),
          ),
        ],
      ),
    );
  }
}
