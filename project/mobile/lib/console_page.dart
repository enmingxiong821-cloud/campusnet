/// 控制台外壳：能连上就嵌控制台，连不上就说清「电脑大概是关着的」。
///
/// 直接丢一个 WebView 出去的话，电脑关机时用户看到的是浏览器的
/// 「无法访问此网站」——那句话既不像他自己写的程序，也没告诉他该怎么办。
/// 所以先探活，再决定显示哪个。
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import 'api.dart';
import 'store.dart';
import 'wake_view.dart';
import 'wol.dart';

/// 往网页里注入的 JS 用它把结果送回来。名字随便取，只要两边一致。
const String _bridgeChannel = 'CampusNetBridge';

class ConsolePage extends StatefulWidget {
  const ConsolePage({
    super.key,
    required this.baseUrl,
    required this.store,
    this.pairCode = '',
    required this.onEditSettings,
  });

  final String baseUrl;

  final AppStore store;

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
        ..addJavaScriptChannel(_bridgeChannel, onMessageReceived: _onWakeInfo)
        ..setNavigationDelegate(
          NavigationDelegate(
            // 每次页面加载完都试一次同步网卡 MAC。控制台也是在这时候才把
            // 配对码换成会话的，所以注入的脚本自己会重试几次。
            onPageFinished: (_) => _syncWakeInfo(),
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

  /// 在页面自己的上下文里读网卡 MAC，存到本地供关机后唤醒用。
  ///
  /// 走这条路而不是 APP 直接请求，是因为 `/api/power/info` 要鉴权，而
  /// WebView 里带着控制台自己的会话 Cookie——同源 fetch 自动就带上了，
  /// APP 不必去碰令牌那一套。没登录时它返回非 2xx，脚本什么都不做。
  void _syncWakeInfo() {
    if (widget.store.targets.isNotEmpty) return;
    _controller?.runJavaScript('''
      (function () {
        var ask = function () {
          fetch('/api/power/info', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
              if (d && d.macs && d.macs.length) {
                $_bridgeChannel.postMessage(JSON.stringify({
                  macs: d.macs,
                  broadcast: d.broadcast || ''
                }));
              }
            })
            .catch(function () {});
        };
        ask();
        // 配对码是页面加载之后才去兑换的，那时会话还没建好，第一次多半 401。
        setTimeout(ask, 4000);
        setTimeout(ask, 12000);
      })();
    ''');
  }

  Future<void> _onWakeInfo(JavaScriptMessage message) async {
    if (widget.store.targets.isNotEmpty) return;
    final Object? decoded;
    try {
      decoded = jsonDecode(message.message);
    } on FormatException {
      return;
    }
    if (decoded is! Map) return;
    final macs = decoded['macs'];
    if (macs is! List || macs.isEmpty) return;

    final host = Uri.parse(widget.baseUrl).host;
    final broadcast = '${decoded['broadcast'] ?? ''}'.trim();
    // 只记第一个。后端取的是 getmac 的输出、把「已连接」的排在前面，但虚拟网卡
    // （Hyper-V / VMware）也自称已连接，所以这里可能记错——离线页上把 MAC 原样
    // 显示出来，就是为了让用户能一眼看出不对并改掉。
    final first = '${macs.first}'.trim();
    if (first.isEmpty) return;
    try {
      await widget.store.saveTargets([
        WakeTarget(name: host, mac: normalizeMac(first), broadcast: broadcast),
      ]);
    } on WolError {
      return;
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
    return WakeView(
      baseUrl: widget.baseUrl,
      store: widget.store,
      onRetry: _probe,
      // 叫醒之后先探活再建 WebView：控制器可能还没建过（APP 一开就是离线态）。
      onAwoke: _probe,
    );
  }
}
