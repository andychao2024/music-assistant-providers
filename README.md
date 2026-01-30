# music-assistant-providers

#### 介绍
适合国人使用Music Assistant 最强插件，支持歌手、专辑、图片、歌词自动补全 


![输入图片说明](https://foruda.gitee.com/images/1769740086725691994/237177b6_8703437.png "歌手简介.png")
![输入图片说明](https://foruda.gitee.com/images/1769739476707515458/3f21a549_8703437.jpeg "歌手演示.jpg")
![输入图片说明](https://foruda.gitee.com/images/1769739440639151325/cd547302_8703437.png "唱片集.png")
![输入图片说明](https://foruda.gitee.com/images/1769739523993717956/c4e2b9a8_8703437.jpeg "演示1.jpg")
![输入图片说明](https://foruda.gitee.com/images/1769739825687398290/8c025104_8703437.png "歌词演示.png")

#### 插件代码是本人学习 Pytone时编写及豆包修正生成的，歌曲元数据来源需要配合云音乐API使用。 插件代码绿色开源，符合Music Assistant插件开发Demo。

- 主要解决国内使用时Music Assistant的UI歌手图片经常获取不了问题
- 本次插件由 musicbrainz魔改版 和 netease_metadata同共使用（也可以独立使用，需要原版musicbrainz正确识别到数据后才能触发插件）
- 为什么要魔改musicbrainz，因为Music Assistant默认先识别这个插件更新元数据信息，又不能禁用此插件。我们国人在使用时会经常遇到网络等各种问题识别不了元数据，从而触发不了其它插件运作。经过源码分析出原因由于元数据在musicbrainz上有各国语言版本出现，对应的简体元数据比较少，所以经常获取不了数据。如果大家有喜欢的歌手或歌曲也可以去平台上补充下。MusicBrainz,开放音乐百科全书,最全的音乐元信息数据库 https://musicbrainz.org/


#### 安装教程

1.  xxxx
2.  xxxx
3.  xxxx

#### 使用说明

1.  xxxx
2.  xxxx
3.  xxxx

#### 参与贡献

1.  Fork 本仓库
2.  新建 Feat_xxx 分支
3.  提交代码
4.  新建 Pull Request