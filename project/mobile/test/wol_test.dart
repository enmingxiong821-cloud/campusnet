/// 魔术包的格式是 APP 与后端之间唯一的硬约定：多发一个字节、少发一个字节，
/// 网卡都不认。后端那份由 `project/backend/tests/test_power.py` 钉着，
/// 这份钉住 Dart 侧——两边任何一边改动，这里必须同时改，否则测试就该红。
library;

import 'package:campusnet_mobile/store.dart';
import 'package:campusnet_mobile/wol.dart';
import 'package:flutter_test/flutter_test.dart';

/// 由后端 `power.py` 的 `magic_packet('AA:BB:CC:DD:EE:FF')` 实际跑出来的结果，
/// 不是照规则推导的——推导出来的向量验不出「两边理解不一致」。
const List<int> _backendVector = [
  255, 255, 255, 255, 255, 255, //
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
  170, 187, 204, 221, 238, 255, 170, 187, 204, 221, 238, 255,
];

void main() {
  group('魔术包', () {
    test('与后端 magic_packet() 逐字节一致', () {
      expect(magicPacket('AA:BB:CC:DD:EE:FF'), _backendVector);
    });

    test('长度固定 102 字节', () {
      expect(magicPacket('00:11:22:33:44:55').length, 102);
    });

    test('前 6 字节是同步流', () {
      final packet = magicPacket('00:11:22:33:44:55');
      expect(packet.sublist(0, 6), everyElement(0xFF));
    });

    test('MAC 重复 16 次', () {
      final packet = magicPacket('00:11:22:33:44:55');
      const once = [0x00, 0x11, 0x22, 0x33, 0x44, 0x55];
      for (var repeat = 0; repeat < 16; repeat++) {
        expect(
          packet.sublist(6 + repeat * 6, 6 + repeat * 6 + 6),
          once,
          reason: '第 $repeat 次重复不对',
        );
      }
    });

    test('端口与后端一致', () {
      expect(wolPort, 9);
    });
  });

  group('MAC 归一化', () {
    test('冒号、短横线、空格、裸十六进制都认', () {
      for (final input in [
        'AA:BB:CC:DD:EE:FF',
        'aa-bb-cc-dd-ee-ff',
        'aabbccddeeff',
        'AA BB CC DD EE FF',
        'aa:bb-cc:dd-ee:ff',
      ]) {
        expect(normalizeMac(input), 'AABBCCDDEEFF', reason: input);
      }
    });

    test('长度不对就报错，不用错地址发包', () {
      for (final bad in ['', 'AA:BB:CC:DD:EE', 'AA:BB:CC:DD:EE:FF:00', 'zz']) {
        expect(() => normalizeMac(bad), throwsA(isA<WolError>()), reason: bad);
      }
    });
  });

  group('广播地址推断', () {
    test('按 /24 取定向广播', () {
      expect(guessBroadcast('192.168.1.5'), '192.168.1.255');
      expect(guessBroadcast('10.20.30.40'), '10.20.30.255');
    });

    test('不是点分 IPv4 就返回 null，交给调用方只打全局广播', () {
      for (final host in ['pc.tailnet.ts.net', 'localhost', '', '1.2.3']) {
        expect(guessBroadcast(host), isNull, reason: host);
      }
    });

    test('越界的段不猜', () {
      expect(guessBroadcast('192.168.1.999'), isNull);
    });
  });

  group('控制台地址归一化', () {
    test('缺 scheme 补 http，缺端口补 2268', () {
      expect(normalizeBaseUrl('192.168.1.5:2268'), 'http://192.168.1.5:2268');
      expect(normalizeBaseUrl('192.168.1.5'), 'http://192.168.1.5:2268');
      expect(normalizeBaseUrl('10.0.0.2:8080'), 'http://10.0.0.2:8080');
    });

    test('两侧空白与末尾斜杠不掉进地址里', () {
      expect(normalizeBaseUrl('  http://192.168.1.5:2268/  '), 'http://192.168.1.5:2268');
    });

    test('https 与 Tailscale 域名照收', () {
      expect(normalizeBaseUrl('https://pc.tailnet.ts.net'), 'https://pc.tailnet.ts.net:2268');
    });

    test('看不懂的地址要报错而不是存下去', () {
      for (final bad in ['', '   ', 'ftp://192.168.1.5', '不是地址']) {
        expect(() => normalizeBaseUrl(bad), throwsFormatException, reason: bad);
      }
    });
  });
}
