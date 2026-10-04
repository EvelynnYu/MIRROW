共域素材库（内站、手机、网站共用）

在此目录放 PNG/JPG/WebP/GIF，然后在 catalog.json 的 assets 数组添加：
{"id":"my-frame","name":"我的动态头像框","category":"frame","file":"my-frame.gif"}

category 可用 frame（头像框）、background（背景）、card（背景卡）、exhibit（展品）。
id 只用英文、数字、短横线或下划线；不要重复。file 只填本目录中的文件名。
不允许网址、绝对路径、目录跳转、SVG/HTML 或链接文件。
每张图最多10MB；GIF最多120帧。上传会清理元数据并检查尺寸。
刷新页面后，在编辑器的“从素材库选择”中使用。选择时复制到当前身份物料库，
日后更改或删除这里的素材文件，不会破坏已保存的装饰和礼物。
只把愿意分发给朋友的素材放进这个目录；打包时可一起分发。
内置 themes / frames 对应代码样式；新增图片通常只需编辑 assets，不需改样式。
