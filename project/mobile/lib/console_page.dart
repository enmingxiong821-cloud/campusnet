/// 控制台外壳：能连上就嵌控制台，连不上就说清「电脑大概是关着的」。
///
/// 直接丢一个 WebView 出去的话，电脑关机时用户看到的是浏览器的
/// 「无法访问此网站」——那句话既不像他自己写的程序，也没告诉他该怎么办。
/// 所以先探活，再决定显示哪个。
library;

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import 'api.dart';

class ConsolePage extends StatefulWidget {
  const ConsolePage({
    super.key,
    required this.baseUrl,
    this.pairCode = '',
    required this.onEditSettings,
  });

  final String baseUrl;

  /// 首次配对时从配置页带过来的 6 位码，控制台会自己认 `#pair=` 段。
  final String pairCode;

  final VoidCallback onEditSettings;

  @override
  State<ConsolePage> createState() => _ConsolePageState();
}

class _ConsolePageState extends State<ConsolePage> {
  WebViewController? _controller;
  bool _probing = true;
  bool _reachable = false;

  @override
  void initState() {
    super.initState();
    _probe();
  }

  String get _initialUrl {
    final code = widget.pairCode;
    return code.isEmpty ? widget.baseUrl : '${widget.baseUrl}/#pair=$code';
  }

  Future<void> _probe() async {
    setState(() => _probing = true);
    final reachable = await ping(widget.baseUrl);
    if (!mounted) return;
    setState(() {
      _probing = false;
      _reachable = reachable;
    });
    if (reachable && _controller == null) {
      _controller = WebViewController()
        ..setJavaScriptMode(JavaScriptMode.unrestricted)
        ..setNavigationDelegate(
          NavigationDelegate(
            // 页面自己加载失败（服务中途停了、手机切了网）时退回离线态，
            // 而不是把错误页留在屏幕上。
            onWebResourceError: (error) {
              if (error.isForMainFrame ?? true) {
                _backToOffline();
              }
            },
          ),
        )
        ..loadRequest(Uri.parse(_initialUrl));
    }
  }

  void _backToOffline() {
    if (!mounted || !_reachable) return;
    setState(() => _reachable = false);
  }

  Future<void> _reload() async {
    // 不能信上一次探活的结果：电脑刚关机时 _reachable 还是 true，而静态页会被
    // WebView 从 HTTP 缓存里直接拿出来渲染，onWebResourceError 根本不触发——
    // 用户看到的是控制台自己的「会话已失效」，而不是「电脑关着」。
    // 探一次才作数，代价是一个免鉴权的 /api/health。
    setState(() => _probing = true);
    final reachable = await ping(widget.baseUrl);
    if (!mounted) return;
    if (!reachable) {
      setState(() {
        _probing = false;
        _reachable = false;
      });
      return;
    }
    if (_controller == null) {
      await _probe();
      return;
    }
    setState(() {
      _probing = false;
      _reachable = true;
    });
    await _controller!.reload();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) async {
        if (didPop) return;
        final controller = _controller;
        if (_reachable && controller != null) {
          final canGoBack = await controller.canGoBack();
          if (canGoBack) {
            await controller.goBack();
            return;
          }
        }
        if (context.mounted) Navigator.of(context).pop();
      },
      child: Scaffold(
        appBar: AppBar(
          title: const Text('校园网自连'),
          actions: [
            IconButton(
              onPressed: _probing ? null : _reload,
              icon: const Icon(Icons.refresh),
              tooltip: '刷新',
            ),
            IconButton(
              onPressed: widget.onEditSettings,
              icon: const Icon(Icons.settings),
              tooltip: '设置',
            ),
          ],
        ),
        body: _buildBody(),
      ),
    );
  }

  Widget _buildBody() {
    if (_probing) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_reachable && _controller != null) {
      return WebViewWidget(controller: _controller!);
    }
    return _OfflineView(baseUrl: widget.baseUrl, onRetry: _probe);
  }
}

class _OfflineView extends StatelessWidget {
  const _OfflineView({required this.baseUrl, required this.onRetry});

  final String baseUrl;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.power_off,
              size: 56,
              color: theme.colorScheme.onSurfaceVariant,
            ),
            const SizedBox(height: 16),
            Text('连不上那台电脑', style: theme.textTheme.titleLarge),
            const SizedBox(height: 8),
            Text(
              baseUrl,
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
            const SizedBox(height: 20),
            Text(
              '电脑关机时这是正常的——远程唤醒由下一步的「开机」功能负责。\n'
              '如果电脑开着，检查它是否以 --host 0.0.0.0 启动，'
              '以及防火墙是否放行了该端口。',
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium?.copyWith(height: 1.5),
            ),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('重试'),
            ),
          ],
        ),
      ),
    );
  }
}
