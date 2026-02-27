#### 📖介绍

适合国人使用 Music Assistant 最强插件，支持歌手（简介）、专辑、图片、歌词自动补全 


![歌手简介](https://foruda.gitee.com/images/1769740086725691994/237177b6_8703437.png "歌手简介.png")
![歌手演示](https://foruda.gitee.com/images/1769739476707515458/3f21a549_8703437.jpeg "歌手演示.jpg")
![唱片集](https://foruda.gitee.com/images/1769739440639151325/cd547302_8703437.png "唱片集.png")


#### 🎯插件代码是本人学习Pytone时编写及豆包修正生成的，歌曲元数据来源需要配合云音乐API使用。 插件代码绿色开源，符合Music Assistant插件开发Demo。

- 主要解决国内使用时Music Assistant的UI歌手图片经常获取不了问题
- 本次插件由 musicbrainz魔改版 和 netease_metadata同共使用（也可以独立使用，需要原版musicbrainz正确识别到数据后才能触发插件）
- 为什么要魔改musicbrainz，因为Music Assistant默认先识别这个插件更新元数据信息，又不能禁用此插件。我们国人在使用时会经常遇到网络等各种问题识别不了元数据，从而触发不了其它插件运作。经过源码分析出原因由于元数据在musicbrainz上有各国语言版本出现，对应的简体元数据比较少，所以经常获取不了数据。如果大家有喜欢的歌手或歌曲也可以去平台上补充下。MusicBrainz,开放音乐百科全书,最全的音乐元信息数据库。

### 🏗️ 项目结构

```
main/
├── musicbrainz/    #魔改版
├── netease_metadata/ #元数据补全插件
├── netease_lyrics/     # 歌词插件
└── README.md         # 说明文档
```

### 📚使用教程

1. docker 版使用教程


2. home-assistant 加载项使用教程


###  :tw-26a0:提醒
本插件需要依赖网易云音乐 API Enhanced,请先自行部署
https://gitee.com/a1_panda/api-enhanced

###  :speech_balloon: 参与贡献

欢迎所有形式的贡献，包括但不限于：
1. 🐛 提交 Bug 报告
2. 💡 提出新功能建议
3. 📝 改进文档
4. 🔧 提交代码修复或新功能

###   ⚠️ 免责声明
本项目仅供学习和研究使用，使用本项目所产生的一切后果由使用者自行承担。请遵守相关法律法规，不得用于非法用途。

