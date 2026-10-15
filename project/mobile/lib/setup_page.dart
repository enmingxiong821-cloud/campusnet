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
  bool _checking = false;
  String _error = '';

  @override
  void dispose() {
    _urlController.dispose();
    _pairController.dispose();
    super.dispose();
  }

  Future<void> _connect() async {
    final String baseUrl;
    try {
      baseUrl = normalizeBaseUrl(_urlController.text);
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
      await _save(baseUrl);
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
          '· 手机是否和电脑在同一个 Wi-Fi 下',
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
      await _save(baseUrl);
    }
  }

  Future<void> _save(String baseUrl) async {
    await widget.store.saveBaseUrl(baseUrl);
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
