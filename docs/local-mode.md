# SpeakEasy 本地模式（无服务器）使用说明

这个版本不需要服务器、不需要域名：**所有数据存在手机上，云端只负责三件事** ——
DeepSeek 生成对话、SiliconFlow 合成语音 / 识别语音。Key 只保存在本机。

## 一、安装

- 手机安装 `SpeakEasy-<版本>.apk`（允许“安装未知来源应用”）。
- 电脑验收可打开 `http://127.0.0.1:8123`（本地构建的 Web 版，功能相同，麦克风只在电脑上可用）。

## 二、第一次使用（3 步）

1. **设置 → 本地与密钥**
   - `DeepSeek Key`：对话用（必填）
   - `SiliconFlow Key`：语音识别 + 语音合成（必填）
   - `Fish Audio Key`：可选，留空即可
   - 点「保存到本机」，再点「**测试声音**」确认能听到测试语音。
2. **我 → 右上角铅笔**：填昵称、目标语言，保存。
   - 保存后提示「正在匹配语伴，约 20 秒」，随后「新的朋友」会出现 2 位好友。
3. **通过好友 → 聊天**：可以发文字、发语音、点电话。
   - 发现 → 动态：可以「让好友们发几条」，看好友动态、评论、翻译。

## 三、Key 从哪来

| Key | 获取方式 |
|---|---|
| DeepSeek | https://platform.deepseek.com → API keys |
| SiliconFlow | https://cloud.siliconflow.cn → API 密钥（同一把用于 TTS + STT） |
| Fish（可选） | https://fish.audio → API（手机直连通常不可用，建议留空） |

> 手机端请求走 Android 原生网络层（CapacitorHttp），不受网页跨域限制；
> Fish 在部分网络环境下不可达，所以默认用 SiliconFlow 的 CosyVoice 发音。

## 四、数据与迁移

所有数据（资料 / 好友 / 聊天 / 动态 / 表达本 / 记忆）都在本机 IndexedDB。

- **导出数据**：设置 →「导出数据」，生成 `speakeasy-backup-日期.json`
- **导入数据**：新手机装好后，设置 →「导入数据」，选择备份文件，自动刷新
- **清空数据**：设置 →「清空数据」（不可撤销）

导出的备份**不包含 Key**，换手机后需要重新填一次 Key。

## 五、常见问题

| 现象 | 处理 |
|---|---|
| 测试声音失败 | 检查 SiliconFlow Key 是否填对、网络是否可用 |
| 保存资料后一直没好友 | 捏人需 20–30 秒，稍等；仍无则看是否 Key 失效 |
| 语音消息没声音 | 手机是否静音/媒体音量为 0；荣耀机型已用 WebAudio 播放 |
| 通话没声音 | 先确认能听到「测试声音」；再确认接通后是否有招呼声 |
| 想换 Key | 设置里直接改，保存即可，不需要重装 |
| 想换手机 | 旧机导出 → 新机导入 + 重新填 Key |

## 六、开发 / 重新打包

```bash
# 1) 构建本地模式前端
cd frontend
NEXT_PUBLIC_LOCAL_MODE=1 NEXT_PUBLIC_API_BASE= npm run build:backend

# 2) 同步到 Android 工程
cd ../mobile
npx cap sync android

# 3) 打包（debug 可直接安装；release 需签名）
cd android
./gradlew assembleDebug     # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease   # 需配置 signingConfigs
```

Web 验收服务：

```bash
python scripts/web-local-server.py 8123
# 打开 http://127.0.0.1:8123
```

服务器模式仍然保留：构建时不加 `NEXT_PUBLIC_LOCAL_MODE=1`，即连接原 FastAPI 后端。
调试入口（仅本地模式）：浏览器控制台 `window.__w4debug`。

## 七、Fish 音色库（可选，App 内可用）

在「本地与密钥」里填 Fish Audio Key 后：

- 新建好友时，会从 Fish 在线音色库里按性别随机挑一个音色；
- 好友资料卡 →「TA 的声音」下拉框里会多出 **Fish 音色库（N 个在线音色）** 分组，可逐个试听、随机、更换；
- 合成走 Fish 的 /v1/tts，音色比内置 8 个丰富得多。

注意两点：

1. **浏览器的 Web 版用不了 Fish**：Fish 接口不返回跨域头（预检 404），
   这是浏览器安全策略，挂 VPN 也解决不了；Web 版会自动回落到内置音色。
2. **App（APK）内可以正常用**：App 的网络请求走 Android 原生层
   （CapacitorHttp），不受浏览器跨域限制；但需要手机能访问 pi.fish.audio
   （国内网络通常需要代理/VPN）。
3. 如果 Fish 请求失败（没网/Key 错），会自动回落到内置 CosyVoice 音色，不会没声音。
