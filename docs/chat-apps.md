# Chat Apps

将 Erza 接入你常用的聊天平台。当前内置以下频道适配器：

| 频道 | 接入方式 |
|---------|----------|
| **飞书 (Feishu)** | App ID + App Secret，支持扫码登录 |
| **钉钉 (DingTalk)** | App Key + App Secret |
| **企业微信 (Wecom)** | Bot ID + Bot Secret |
| **微信 (Weixin)** | 扫码登录（`erza channels login weixin`） |
| **QQ** | App ID + App Secret |
| **WebSocket / WebUI** | 内置浏览器 UI，零配置可用 |

想要接入其他平台？参考 [频道插件指南](./channel-plugin-guide.md) 自行实现。

## 通用配置

所有频道配置统一放在 `~/.erza/config.json` 的 `channels` 字段下。可以使用 `${VAR_NAME}` 引用环境变量，避免把密钥写入配置文件：

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "${FEISHU_APP_ID}",
      "appSecret": "${FEISHU_APP_SECRET}"
    },
    "websocket": {
      "enabled": true
    }
  }
}
```

频道级别的通用设置（如 `sendProgress`、`sendToolHints`、`sendMaxRetries`）见 [Configuration: Channel Settings](./configuration.md#channel-settings)。

## 飞书 (Feishu)

**1. 创建飞书应用**

- 进入 [飞书开放平台](https://open.feishu.cn/) 创建企业自建应用
- 记录 `App ID` 与 `App Secret`
- 在「事件订阅」中配置消息接收地址，或启用长连接模式

**2. 配置**

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "${FEISHU_APP_ID}",
      "appSecret": "${FEISHU_APP_SECRET}"
    }
  }
}
```

**3. 扫码登录（可选）**

Erza 支持飞书扫码登录流程，运行后通过 WebUI 引导扫码即可自动完成配置并持久化。

## 钉钉 (DingTalk)

**1. 创建钉钉应用**

- 进入 [钉钉开放平台](https://open.dingtalk.com/) 创建企业内部应用
- 记录 `App Key` 与 `App Secret`
- 开启机器人与消息推送能力

**2. 配置**

```json
{
  "channels": {
    "dingtalk": {
      "enabled": true,
      "appKey": "${DINGTALK_APP_KEY}",
      "appSecret": "${DINGTALK_APP_SECRET}"
    }
  }
}
```

## 企业微信 (Wecom)

**1. 创建企业微信机器人**

- 在企业微信管理后台创建自建应用
- 记录 `Bot ID` 与 `Bot Secret`

**2. 配置**

```json
{
  "channels": {
    "wecom": {
      "enabled": true,
      "botId": "${WECOM_BOT_ID}",
      "botSecret": "${WECOM_BOT_SECRET}"
    }
  }
}
```

## 微信 (Weixin)

通过扫码登录接入个人微信：

```bash
erza channels login weixin
```

扫码成功后凭据自动写入 `~/.erza/config.json`。

> 注意：个人微信接入存在账号风险，仅建议在小号或测试账号上使用。

## QQ

**1. 创建 QQ 机器人**

- 在 [QQ 开放平台](https://q.qq.com/) 注册机器人
- 记录 `App ID` 与 `App Secret`

**2. 配置**

```json
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "${QQ_APP_ID}",
      "appSecret": "${QQ_APP_SECRET}"
    }
  }
}
```

## WebSocket / WebUI

内置频道，**零配置即可使用**。启动网关后通过浏览器访问 WebUI：

```bash
erza gateway
```

默认监听本地端口，支持局域网访问与 Token 鉴权。详见 [Deployment](./deployment.md)。

如需对 WebSocket 频道进行细粒度控制：

```json
{
  "channels": {
    "websocket": {
      "enabled": true,
      "sendToolHints": true
    }
  }
}
```

## 准入控制（allowFrom）

各频道的 `allowFrom` 配置决定谁可以与机器人对话：设置为 `["*"]` 放行所有人，或列出具体的用户 ID（精确匹配）。未配置或为空时拒绝所有发送者。详见 [Configuration](./configuration.md)。
