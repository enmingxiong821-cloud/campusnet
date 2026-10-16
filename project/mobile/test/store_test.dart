/// 本地存储的测试：中转那几个字段的存取语义。
///
/// 这部分容易写错的地方全在边界上——令牌要不要跟着地址一起清、空地址算不算
/// 「配好了」、以及地址归一化那几条手误会变成什么。
library;

import 'package:campusnet_mobile/store.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  group('中转配置', () {
    setUp(() => SharedPreferences.setMockInitialValues({}));

    test('存了地址和令牌才算配好', () async {
      final store = await AppStore.load();
      expect(store.relayReady, isFalse);

      await store.saveRelay(url: 'http://10.0.0.9:2268', token: 'abc');
      expect(store.relayReady, isTrue);

      final reloaded = await AppStore.load();
      expect(reloaded.relayUrl, 'http://10.0.0.9:2268');
      expect(reloaded.relayToken, 'abc');
      expect(reloaded.relayReady, isTrue);
    });

    test('只有地址没有令牌不算配好', () async {
      final store = await AppStore.load();
      await store.saveRelay(url: 'http://10.0.0.9:2268', token: '');
      expect(store.relayReady, isFalse);
    });

    test('清空地址时把令牌一并删掉，不留一份用不上的凭据', () async {
      SharedPreferences.setMockInitialValues({
        'relay_url': 'http://10.0.0.9:2268',
        'relay_token': 'abc',
      });
      final store = await AppStore.load();
      expect(store.relayReady, isTrue);

      await store.saveRelay(url: '', token: 'abc');

      expect(store.relayReady, isFalse);
      final reloaded = await AppStore.load();
      expect(reloaded.relayUrl, '');
      expect(reloaded.relayToken, '');
    });

    test('clear 会把中转配置一起清掉', () async {
      SharedPreferences.setMockInitialValues({
        'base_url': 'http://10.0.0.5:2268',
        'relay_url': 'http://10.0.0.9:2268',
        'relay_token': 'abc',
      });
      final store = await AppStore.load();

      await store.clear();

      expect(store.baseUrl, '');
      expect(store.relayReady, isFalse);
      final reloaded = await AppStore.load();
      expect(reloaded.baseUrl, '');
      expect(reloaded.relayUrl, '');
      expect(reloaded.relayToken, '');
    });

    test('存坏的目标列表不该让 APP 起不来', () async {
      SharedPreferences.setMockInitialValues({'wake_targets': '{不是 JSON'});
      final store = await AppStore.load();
      expect(store.targets, isEmpty);
    });
  });

  group('normalizeBaseUrl', () {
    test('不带 scheme 时补 http，端口留空补默认端口', () {
      expect(normalizeBaseUrl('192.168.1.5'), 'http://192.168.1.5:2268');
      expect(normalizeBaseUrl('192.168.1.5:3000'), 'http://192.168.1.5:3000');
    });

    test('末尾斜杠与路径都保留得住', () {
      expect(normalizeBaseUrl('http://a.example.com/'), 'http://a.example.com:2268');
      expect(
        normalizeBaseUrl('https://a.example.com:8443/x'),
        'https://a.example.com:8443/x',
      );
    });

    test('不是地址的手误要报错，不能被百分号转义后当成合法地址存下来', () {
      expect(
        () => normalizeBaseUrl('这不是地址'),
        throwsA(isA<FormatException>()),
      );
      expect(() => normalizeBaseUrl('   '), throwsA(isA<FormatException>()));
    });

    test('非 http/https 的 scheme 拦下来', () {
      expect(
        () => normalizeBaseUrl('ftp://192.168.1.5'),
        throwsA(isA<FormatException>()),
      );
    });
  });
}
