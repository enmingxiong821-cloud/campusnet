/// 校园网自连 · 移动端外壳。
///
/// 这个 APP 不做控制台已经做过的事——它只补两样浏览器给不了的：
/// 一个桌面图标式的入口，以及在电脑关着的时候把它叫醒的能力。
library;

import 'package:flutter/material.dart';

import 'console_page.dart';
import 'setup_page.dart';
import 'store.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final store = await AppStore.load();
  runApp(CampusNetApp(store: store));
}

class CampusNetApp extends StatefulWidget {
  const CampusNetApp({super.key, required this.store});

  final AppStore store;

  @override
  State<CampusNetApp> createState() => _CampusNetAppState();
}

class _CampusNetAppState extends State<CampusNetApp> {
  late String _baseUrl = widget.store.baseUrl;
  String _pairCode = '';

  /// 每次从设置页重进控制台都换一个 key，逼出一个全新的 WebView——
  /// 否则换了地址之后，旧页面还留在那儿。
  int _consoleGeneration = 0;

  void _openConsole(String baseUrl, String pairCode) {
    setState(() {
      _baseUrl = baseUrl;
      _pairCode = pairCode;
      _consoleGeneration++;
    });
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '校园网自连',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: const Color(0xFF0F766E)),
      home: _baseUrl.isEmpty
          ? SetupPage(store: widget.store, onDone: _openConsole)
          : ConsolePage(
              key: ValueKey(_consoleGeneration),
              baseUrl: _baseUrl,
              pairCode: _pairCode,
              onEditSettings: () => setState(() => _baseUrl = ''),
            ),
    );
  }
}
