# 第三方来源与许可范围

本模块自有代码使用 `LICENSE` 的 MIRROW MIT。下列内容分别保留其原许可，不因放在同一仓库改授 MIT。

## AionsHome Visitor Lounge 接入

- 来源：https://github.com/death34018-hue/AionsHome
- 范围：Visitor Key、稳定 visitor ID、人机关联、接待设置与本家受保护管理页面的接入／兼容合同。完整上游宿主及运行数据不在本模块中。
- MIT，Copyright (c) 2026 death34018-hue；全文在 `LICENSE-AionsHome.txt`。
- 不能把整个共域白名单称为直接复制自上游；本模块包含 MIRROW 领域实现和 AionsHome 接口适配。后续若引入其他直接派生文件，应逐文件补充归属。

## Kenney · Fantasy UI Borders

- 来源：https://kenney.nl/assets/fantasy-ui-borders
- 六张未修改 PNG：`backend/social_feed/decor_presets/kenney-{card,frame}-{000,006,014}.png`。
- CC0 1.0。保留上游告知 `Kenney-CC0.txt` 与 `SOURCES.txt`；CC0 原文 https://creativecommons.org/publicdomain/zero/1.0/legalcode 。

## Ma Shan Zheng

- 来源：https://github.com/google/fonts/tree/main/ofl/mashanzheng
- `backend/social_feed/decor_presets/MaShanZheng-Regular.ttf`，原字体未修改。
- SIL OFL 1.1，Copyright 2018 The Ma Shan Zheng Project Authors；全文 `MaShanZheng-OFL.txt` 随字体保留。

## Great Vibes

- 来源：https://github.com/google/fonts/tree/main/ofl/greatvibes
- `backend/social_feed/decor_presets/GreatVibes-Regular.ttf`，原字体未修改。
- SIL OFL 1.1，Copyright 2015 The Great Vibes Pro Project Authors；全文 `GreatVibes-OFL.txt` 随字体保留。

## 包管理器依赖

React、React DOM、Vite、TypeScript、FastAPI、Pydantic、HTTPX、Uvicorn、Pillow、pytest 等由部署者按自身锁文件安装；本副本不内嵌 wheel、node_modules 或依赖构建产物。可选 ffmpeg／ffprobe 与 GIF 平滑依赖由宿主提供，不自动下载或分发。
