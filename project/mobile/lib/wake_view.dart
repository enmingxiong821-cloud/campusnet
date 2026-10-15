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
  });

  final String baseUrl;
  final AppStore store;

  /// 重新探活一次（用户点「重新检测」）。
  final Future<void> Function() onRetry;

  /// 探到机器起来了。由外层切到控制台。
  final Future<void> Function() onAwoke;

  @override
  State<WakeView> createState() => _WakeViewState();
}

class _WakeViewState extends State<WakeView> {
  bool _waiting = false;
  int _waited = 0;
  String _error = '';

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

  Future<void> _wake(WakeTarget target) async {
    final generation = ++_generation;
    setState(() {
      _waiting = true;
      _waited = 0;
      _error = '';
    });

    // 定向广播优先，全局广播由 wake() 自己补上，两个都发才有兜底。
    final directed = directedBroadcastFor(_host, target.broadcast);
    final List<String> sent;
    try {
      sent = await wake(target.mac, broadcast: directed);
    } on WolError catch (error) {
      if (!mounted || generation != _generation) return;
      setState(() {
        _waiting = false;
        _error = error.message;
      });
      return;
    }
    if (!mounted || generation != _generation) return;

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
    setState(() {
      _waiting = false;
      _error = '唤醒包已经发出去了（${sent.join('、')}），但等了 '
          '${wakeWaitLimit.inSeconds} 秒还没等到它应答。\n'
          '可以再发一次。一直没反应时，常见原因有三个：网卡的高级设置里没开'
          '「网络唤醒」、BIOS 里没开 WOL、或者手机和电脑不在同一个二层网络里。';
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
            '已等待 $_waited 秒（最多等 ${wakeWaitLimit.inSeconds} 秒）',
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
      ],
    );
  }
}
