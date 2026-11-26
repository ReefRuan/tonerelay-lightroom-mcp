# Adobe Lightroom 桌面端（实验）

这个目录只在检出的仓库里使用。它不是安装步骤，也不改 `bridge start`、`lightroom-plugin install` 或端口 `17777`。

正式名称是 **Adobe Lightroom**（图标 **Lr**）。Lightroom Classic（图标 **LrC**）继续走现有插件。

只有同时满足下面两步才启动适配器：

1. 当前会话用的是这份仓库。
2. 用户明确要使用 Adobe Lightroom，而不是 Lightroom Classic。

## 现在能做什么

可以读取当前打开的照片的显影参数。控制器接口没有稳定的照片 ID，`cloud:active` 只表示「此刻打开的那张」。因此 `apply_candidate`、`reset_to_baseline` 和退出时的基线恢复都会拒绝写入，直到照片身份能绑住。

适配器发现目标是 `127.0.0.1:17777` 时直接退出。默认队列是 `127.0.0.1:17778`，不要把它写进已安装的 `config.json` 或客户端 MCP 配置。

## 单独启动

在 Lightroom 的首选项里启用外部控制器，退出并重新打开，确认弹出的是 **Lr** 图标的应用。然后在仓库里另开一条队列：

```sh
cd services/lr-ops
LR_XMP_MIMIC_BRIDGE_PORT=17778 \
LR_XMP_MIMIC_RUNTIME_DATA_ROOT=/tmp/lr-ops-lightroom-desktop \
  python3 -m lr_ops serve
```

另一个终端：

```sh
cd bridges/lightroom-cloud
LR_OPS_URL=http://127.0.0.1:17778 node adapter.mjs
```

第一次连接会弹出配对确认。Classic 插件如果还在轮询 `17777`，不要把上面的端口改成 `17777`。

## 测试

```sh
node --test test/adapter.test.mjs test/shutdown.test.mjs
```

`verify.mjs` 只在本机已经单独启动这条队列、并且打开的是 Adobe Lightroom 时使用。它检查读取，并确认写入被拒绝、照片参数没有变化。
