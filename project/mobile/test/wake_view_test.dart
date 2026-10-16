/// 离线页的静态部分：中转那一行有没有按配置如实显示。
///
/// 只测渲染，不点「唤醒」——那条路要真的发广播和打网络，属于模拟器验的范围。
library;

import 'package:campusnet_mobile/store.dart';
import 'package:campusnet_mobile/wake_view.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

Future<AppStore> _storeWith({
  required List<WakeTarget> targets,
  String relayUrl = '',
  String relayToken = '',
}) async {
  SharedPreferences.setMockInitialValues({});
  final store = await AppStore.load();
  await store.saveBaseUrl('http://10.0.0.5:2268');
  await store.saveTargets(targets);
  await store.saveRelay(url: relayUrl, token: relayToken);
  return store;
}

Widget _wrap(AppStore store, {VoidCallback? onEditRelay}) => MaterialApp(
      home: Scaffold(
        body: WakeView(
          baseUrl: 'http://10.0.0.5:2268',
          store: store,
          onRetry: () async {},
          onAwoke: () async {},
          onEditRelay: onEditRelay ?? () {},
        ),
      ),
    );

void main() {
  const target = WakeTarget(name: '宿舍机', mac: 'AC:45:EF:49:B7:F4');

  testWidgets('没配中转时给出说明与配置入口', (tester) async {
    var opened = 0;
    final store = await _storeWith(targets: [target]);
    await tester.pumpWidget(_wrap(store, onEditRelay: () => opened++));

    expect(find.textContaining('不在同一个 Wi-Fi'), findsOneWidget);
    await tester.tap(find.text('配置中转设备'));
    expect(opened, 1);
    // 配了地址没配令牌等于没配，所以按钮还该写着「配置」而不是「改设置」。
    expect(find.text('改中转设置'), findsNothing);
  });

  testWidgets('配好中转后显示是谁代发，并给改设置的入口', (tester) async {
    var opened = 0;
    final store = await _storeWith(
      targets: [target],
      relayUrl: 'http://10.0.0.9:2268',
      relayToken: 'abc',
    );
    await tester.pumpWidget(_wrap(store, onEditRelay: () => opened++));

    expect(find.text('中转设备：http://10.0.0.9:2268'), findsOneWidget);
    await tester.tap(find.text('改中转设置'));
    expect(opened, 1);
  });

  testWidgets('没记下 MAC 时说清怎么补，不去谈唤醒', (tester) async {
    final store = await _storeWith(targets: const []);
    await tester.pumpWidget(_wrap(store));

    expect(find.textContaining('还没记下这台电脑的网卡 MAC'), findsOneWidget);
    expect(find.text('唤醒这台电脑'), findsNothing);
    expect(find.text('手动填写 MAC'), findsOneWidget);
  });

  testWidgets('面板里带上「开机准备」的指路', (tester) async {
    final store = await _storeWith(targets: [target]);
    await tester.pumpWidget(_wrap(store));

    expect(find.textContaining('开机准备'), findsOneWidget);
  });
}
