/// 电脑连不上时的那一屏——也是这个 APP 存在的主要理由：在这儿把它叫醒。
///
/// 分两段：上面说清「为什么连不上、该怎么办」，下面才是真正有用的动作。
/// 唤醒成功后自动轮询到它开机，再交回控制台页，用户不用自己去猜什么时候好。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'store.dart';
import 'wol.dart';

/// 一次唤醒最多等多久。
///
/// 真机上从收到魔术包到后端能应答，中间要过 BIOS 自检、Windows 启动、以及
/// 开机自启把服务拉起来，一分半是常见量级。给两分钟，等不到再说。
const Duration wakeWaitLimit = Duration(seconds: 120);

/// 轮询间隔。`ping` 自带 4 秒超时，所以机器没起时一轮实际约 7 秒——
/// 显示给用户的秒数按真实流逝算，不按这个常数累加。
const Duration wakePollStep = Duration(seconds: 3);

class WakeView extends StatefulWidget {
  const WakeView({
    super.key,
    required this.baseUrl,
    required this.store,
    required this.onRetry,
    required this.onAwoke,
    required this.onEditRelay,
  });

  final String baseUrl;
  final AppStore store;

  /// 重新探活一次（用户点「重新检测」）。
  final Future<void> Function() onRetry;

  /// 探到机器起来了。由外层切到控制台。
  final Future<void> Function() onAwoke;

  /// 去配置页改中转设置。多绕一步是刻意的——中转的校验（地址合法、令牌非空）
  /// 只能有一份，放这儿再写一遍迟早跟配置页跑偏。
  final VoidCallback onEditRelay;

  @override
  State<WakeView> createState() => _WakeViewState();
}

class _WakeViewState extends State<WakeView> {
  bool _waiting = false;
  int _waited = 0;
  String _error = '';

  /// 本次唤醒实际走通了哪几条路。等待期间显示出来，超时后也是排查的第一手信息。
  String _routes = '';

  /// 「本次等待」的编号。停止等待、超时、以及组件被销毁都靠它把已经在跑的
  /// 那个循环认出来——否则用户点了「停止等待」之后，上一轮循环还会在后台继续
  /// 轮询，等机器真起来了又把人拽进控制台，而他明明已经放弃了。
  int _generation = 0;

  String get _host => Uri.parse(widget.baseUrl).host;

  WakeTarget? get _target =>
      widget.store.targets.isEmpty ? null : widget.store.targets.first;

  @override
  void dispose() {
    _generation++;
    super.dispose();
  }

  /// 把中转的结果翻译成人话。null 表示压根没走这条路。
  String _relayNote(RelayOutcome? outcome) {
    switch (outcome) {
      case RelayOutcome.ok:
        return '中转设备已代发魔术包';
      case RelayOutcome.notFound:
        return '中转设备上没登记这块网卡——得先去那台机器的控制台，'
            '在「远程开机」里把 ${formatMac(_target?.mac ?? '')} 加进设备表，'
            '它按设备发，不认裸 MAC';
      case RelayOutcome.unauthorized:
        return '中转设备拒绝了令牌。令牌会随它那边重新登录而失效，重抄一遍';
      case RelayOutcome.unreachable:
        return '连不上中转设备（${widget.store.relayUrl}），地址或网络不对';
      case RelayOutcome.failed:
        return '中转设备收到了请求，但没能把包发出去';
      case null:
        return '';
    }
  }

  Future<void> _wake(WakeTarget target) async {
    final generation = ++_generation;
    setState(() {
      _waiting = true;
      _waited = 0;
      _error = '';
      _routes = '';
    });

    // 定向广播优先，全局广播由 wake() 自己补上，两个都发才有兜底。
    final directed = directedBroadcastFor(_host, target.broadcast);
    var localOk = false;
    var localNote = '';
    try {
      final sent = await wake(target.mac, broadcast: directed);
      localOk = true;
      localNote = '同网段广播（发往 ${sent.join('、')}）';
    } on WolError catch (error) {
      localNote = '同网段广播：${error.message}';
    }

    // 中转只要配了就一定打一次，哪怕上面那条「成功」了。原因是 `send()` 返回
    // 102 字节只说明包交给了系统，不说明有人收得到——人不在同一个 Wi-Fi 里时
    // 它照样「成功」，只是广播出了网卡就没了。那种场合唯一有用的是中转，
    // 若因为它「重复」而跳过，用户就只会看着两分钟空等。
    RelayOutcome? relay;
    if (widget.store.relayReady) {
      relay = await relayWake(
        widget.store.relayUrl,
        widget.store.relayToken,
        target.mac,
      );
    }
    // 中转那一步是个 await，回来之后这个组件可能已经被换掉了（用户退出了离线页、
    // 或点了「停止等待」），所以在这儿重新认一次。
    if (!mounted || generation != _generation) return;

    final relayOk = relay == RelayOutcome.ok;
    if (!localOk && !relayOk) {
      setState(() {
        _waiting = false;
        _error = relay == null
            // 连中转都没配，那「发不出去」的原因基本只有一个，直接点破。
            ? '$localNote\n\n魔术包没能发出去。手机不在电脑所在的那个 Wi-Fi 时，'
                '广播出不了你的网络——到「设置」里配一台中转设备，请它代发。'
            : '$localNote\n${_relayNote(relay)}\n\n两条路都没走通，魔术包根本没发出去。';
      });
      return;
    }

    final routes = [
      if (localOk) '同网段广播',
      if (relayOk) '中转设备',
    ].join('、');
    setState(() => _routes = routes);

    final start = DateTime.now();
    while (DateTime.now().difference(start) < wakeWaitLimit) {
      await Future<void>.delayed(wakePollStep);
      if (!mounted || generation != _generation) return;
      setState(() => _waited = DateTime.now().difference(start).inSeconds);
      if (await ping(widget.baseUrl)) {
        if (!mounted || generation != _generation) return;
        await widget.onAwoke();
        return;
      }
    }
    if (!mounted || generation != _generation) return;
    // 中转没成功也不拦住等待（本地发成功了就还有戏），但超时时必须说出来——
    // 用户人可能就在校外，那条路才是他的救命稻草，不说等于让他白等。
    final relayTrouble = (relay != null && relay != RelayOutcome.ok)
        ? '\n另外，${_relayNote(relay)}。'
        : '';
    setState(() {
      _waiting = false;
      _error = '唤醒包已经发出去了（$routes），但等了 '
          '${wakeWaitLimit.inSeconds} 秒还没等到它应答。\n'
          '可以再发一次。一直没反应时，常见原因有三个：网卡的高级设置里没开'
          '「网络唤醒」、BIOS 里没开 WOL、或者手机和电脑不在同一个二层网络里。'
          '$relayTrouble';
    });
  }

  void _stopWaiting() {
    _generation++;
    setState(() {
      _waiting = false;
      _waited = 0;
    });
  }

  Future<void> _editMac() async {
    final current = _target;
    final controller = TextEditingController(
      text: current == null ? '' : formatMac(current.mac),
    );
    final value = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('网卡 MAC'),
        content: TextField(
          controller: controller,
          autofocus: true,
          autocorrect: false,
          decoration: const InputDecoration(
            hintText: 'AA:BB:CC:DD:EE:FF',
            helperText: '控制台的「远程开机」区域能看到本机网卡 MAC',
            border: OutlineInputBorder(),
          ),
          onSubmitted: (text) => Navigator.of(context).pop(text),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(controller.text),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (value == null || !mounted) return;

    final String mac;
    try {
      mac = normalizeMac(value);
    } on WolError catch (error) {
      setState(() => _error = error.message);
      return;
    }
    // 广播地址存空：发的时候按当前控制台地址现算，比存一个可能过期的值可靠。
    await widget.store.saveTargets([WakeTarget(name: _host, mac: mac)]);
    if (!mounted) return;
    setState(() => _error = '');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final target = _target;

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
              widget.baseUrl,
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
            const SizedBox(height: 20),
            Text(
              '如果它开着，检查是否以 --host 0.0.0.0 启动、防火墙是否放行了该端口。\n'
              '如果它关着，下面可以直接把它叫醒。',
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium?.copyWith(height: 1.5),
            ),
            const SizedBox(height: 28),
            const Divider(),
            const SizedBox(height: 20),
            if (target == null)
              _buildNoMac(theme)
            else
              _buildWake(theme, target),
            if (_error.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(
                _error,
                textAlign: TextAlign.center,
                style: TextStyle(color: theme.colorScheme.error, height: 1.5),
              ),
            ],
            const SizedBox(height: 24),
            OutlinedButton.icon(
              onPressed: _waiting ? null : widget.onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('重新检测'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildNoMac(ThemeData theme) {
    return Column(
      children: [
        Text(
          '还没记下这台电脑的网卡 MAC，现在没法发唤醒包。',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodyMedium?.copyWith(height: 1.5),
        ),
        const SizedBox(height: 8),
        Text(
          '它开机后进一次控制台，APP 会自动记下来；也可以在电脑上查到后先手填。',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
            height: 1.5,
          ),
        ),
        const SizedBox(height: 16),
        OutlinedButton.icon(
          onPressed: _editMac,
          icon: const Icon(Icons.edit),
          label: const Text('手动填写 MAC'),
        ),
      ],
    );
  }

  Widget _buildWake(ThemeData theme, WakeTarget target) {
    if (_waiting) {
      return Column(
        children: [
          const SizedBox(
            width: 28,
            height: 28,
            child: CircularProgressIndicator(strokeWidth: 3),
          ),
          const SizedBox(height: 16),
          Text('唤醒包已发出，等它开机…', style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          Text(
            '已等待 $_waited 秒（最多等 ${wakeWaitLimit.inSeconds} 秒）'
            '${_routes.isEmpty ? '' : '\n走了：$_routes'}',
            textAlign: TextAlign.center,
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: 16),
          TextButton(onPressed: _stopWaiting, child: const Text('停止等待')),
        ],
      );
    }
    return Column(
      children: [
        Text(target.name, style: theme.textTheme.titleMedium),
        const SizedBox(height: 4),
        Text(
          formatMac(target.mac),
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 16),
        FilledButton.icon(
          onPressed: () => _wake(target),
          icon: const Icon(Icons.power_settings_new),
          label: const Text('唤醒这台电脑'),
        ),
        const SizedBox(height: 4),
        TextButton(onPressed: _editMac, child: const Text('改 MAC')),
        const SizedBox(height: 12),
        _buildRelayRow(theme),
        const SizedBox(height: 12),
        Text(
          '想做到「通了电就自己开」（比如电脑不在同一个网段，得靠智能插座供电）时，'
          '趁它开着到控制台的「开机准备」跑一次——那里会查快速启动、网卡唤醒、'
          'BIOS 来电自启这些前置条件，能自动修的可以一键修。',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
            height: 1.5,
          ),
        ),
      ],
    );
  }

  /// 中转那一行：配了就显示请谁代发，没配就说明什么时候需要它。
  Widget _buildRelayRow(ThemeData theme) {
    if (!widget.store.relayReady) {
      return Column(
        children: [
          Text(
            '手机和电脑不在同一个 Wi-Fi 时（在外面用流量就是这样），广播出不了'
            '你所在的网络，得靠一台常开的小设备代发。',
            textAlign: TextAlign.center,
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.onSurfaceVariant,
              height: 1.5,
            ),
          ),
          const SizedBox(height: 4),
          TextButton.icon(
            onPressed: widget.onEditRelay,
            icon: const Icon(Icons.dns_outlined),
            label: const Text('配置中转设备'),
          ),
        ],
      );
    }
    return Column(
      children: [
        Text(
          '中转设备：${widget.store.relayUrl}',
          textAlign: TextAlign.center,
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
          ),
        ),
        TextButton(onPressed: widget.onEditRelay, child: const Text('改中转设置')),
      ],
    );
  }
}
