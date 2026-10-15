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

产物在 `build/app/outputs/flutter-apk/app-release.apk`。

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

## 测试

```bash
flutter test
```

`test/wol_test.dart` 用固定向量钉住魔术包字节与后端 `power.magic_packet()` **逐字节一致**
（102 字节、端口 9）。改任一边都必须同步改这份向量，否则测试就该红。

## 目录

| 文件 | 职责 |
|---|---|
| `lib/main.dart` | 入口与路由（配置页 / 控制台页） |
| `lib/setup_page.dart` | 首次配置：地址、配对码、连通性探测 |
| `lib/console_page.dart` | WebView 壳 + 探测 + 离线态 |
| `lib/store.dart` | 地址与唤醒目标的本地持久化 |
| `lib/wol.dart` | 纯 Dart WOL 发送（无插件） |
| `lib/api.dart` | 只调免鉴权接口，鉴权交给 WebView 里的控制台 |
