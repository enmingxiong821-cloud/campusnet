# 校园网自连 · 移动端

Android 外壳：**WebView 嵌现有控制台**，再加一个浏览器给不了的能力——远程唤醒。

## 为什么不是直接开网页

控制台本身已经是移动优先的，手机浏览器打开就能用，但这个 APP 不是为「方便」存在的：

- 电脑**关着**时后端进程没在跑，任何网页都够不着它。只有能往局域网发广播的原生代码
  才叫得醒它（WOL 魔术包）。
- 顺带解决的：桌面图标式入口、不用每次重敲长网址、不用翻历史记录。

控制台那半边完全复用，没有重写任何界面。

## 构建

```bash
cd project/mobile
flutter build apk --release
```

产物在 `build/app/outputs/flutter-apk/app-release.apk`。正式安装包随 GitHub Release
一起发（`release.yml` 的 `build-apk` 作业），一般不用自己编。

发版时版本号由 `--build-name` / `--build-number` 从 git tag 传入，`pubspec.yaml` 里的
版本号只是占位——同一个版本号不留两处，免得迟早对不上：

```bash
flutter build apk --release --build-name=2.2.0 --build-number=1
```

### 本机（Windows）两个坑

1. **Flutter 命令必须带 `--no-version-check`。** 本机 Dart 不读系统代理，校园网对
   `flutter --version` 的版本检查请求是丢包而不是拒绝，不加这个参数会一直挂着。
2. **`flutter analyze` 在本机会崩，改用 `dart analyze`。** 本机仓库放在「校园网自连」
   目录下，路径含中文，`flutter analyze` 走的分析服务器在解析响应时会因为 UTF-8 字节数
   与字符数不一致而抛 `FormatException`。`dart analyze` 不走同一条通道，正常。
   同理，AGP 默认拒绝非 ASCII 路径构建，`android/gradle.properties` 里的
   `android.overridePathCheck=true` 就是为这个加的（CI 与 ASCII 路径下无影响）。

   ```bash
   dart analyze
   ```

## 安装

APK 没走应用商店，需要侧载：手机「设置 → 允许安装未知来源应用」后，直接点开
`app-release.apk` 安装。release 包目前用 debug 密钥签名（够侧载），要正式分发得先配
`android/key.properties` + 密钥库——这两个文件已在 `.gitignore` 里，**绝不能入库**。

## 使用

1. 电脑上跑 `campusnet access`（会打印控制台地址），手机上填进 APP。
2. 也可在电脑控制台点「生成配对码」，把 6 位码填进 APP，省去在手机上登录。
3. 地址连不上时**也允许保存**——配置本来就该能在电脑关机时做完，不然第一台设备得等到
   人在电脑跟前才能配。

### 手机不在同一个 Wi-Fi 时：中转设备

手机的广播出不了它所在的网络。人回到宿舍、手机连上同一个 Wi-Fi 时直发就行；人在外面
用流量时，得请一台**常开的小设备**（宿舍里的小主机、树莓派，跑着同样的服务）代发。
在 APP 的「设置」里填两个字段即可，留空就是不用中转：

| 字段 | 从哪来 |
|---|---|
| 中转设备地址 | 那台设备上跑 `campusnet access`，它会打印地址 |
| 中转设备令牌 | 那台设备上跑 `python utils/run.py token` |

发的时候走的是后端现成的 `/api/devices` + `/api/devices/{id}/wake`，所以**电脑的网卡
得先在那台设备的控制台里登记过**（控制台的「远程开机」区域可以加）——它按设备 id 发，
不认裸 MAC。没登记 APP 会直说，不会让你干等。

这是 APP 里唯一带鉴权的调用：那台设备上并没有页面在跑，借不到控制台的会话，只能显式
带令牌。令牌明文存在 `SharedPreferences` 里；手机与那台设备之间本来就可能是明文 HTTP，
真要防得先给后台上 TLS。`lib/api.dart` 的文件头注释里写了这条边界。

### 唤醒一直不成功时

到控制台的「开机准备」跑一次（电脑开着的时候）。它会查快速启动、网卡唤醒、BIOS 来电
自启这些前置条件，能自动修的（快速启动、网卡唤醒、睡眠超时）一键就能改。

## 测试

```bash
flutter test
```

`test/wol_test.dart` 用固定向量钉住魔术包字节与后端 `power.magic_packet()` **逐字节一致**
（102 字节、端口 9）。改任一边都必须同步改这份向量，否则测试就该红。

`test/api_test.dart` 在回环上起一个假的中转设备，把「查设备表 → 按 id 唤醒」整条走完，
并验到令牌头确实发了、401 不会被当成「地址写错」。`test/store_test.dart` 管中转配置的
存取边界（清地址要连带清令牌、只有地址不算配好），`test/wake_view_test.dart` 管离线页
按配置如实显示。

这两条也是 CI 的两格：`.github/workflows/mobile.yml`（只在 `project/mobile/**` 变动时
触发）与 `release.yml` 打 tag 时的 `build-apk` 作业。mobile.yml **故意不进分支保护的
必过检查**——按路径触发的检查设成必过之后，不碰 APP 的 PR 会永远停在 pending。

## 目录

| 文件 | 职责 |
|---|---|
| `lib/main.dart` | 入口与路由（配置页 / 控制台页） |
| `lib/setup_page.dart` | 首次配置：地址、配对码、中转设备、连通性探测 |
| `lib/console_page.dart` | WebView 壳 + 探测 + 离线态 |
| `lib/wake_view.dart` | 离线页：直发唤醒、中转转发、等它开机 |
| `lib/store.dart` | 地址、唤醒目标、中转配置的本地持久化 |
| `lib/wol.dart` | 纯 Dart WOL 发送（无插件） |
| `lib/api.dart` | 探活用免鉴权接口；`relayWake` 是唯一的带鉴权例外 |
