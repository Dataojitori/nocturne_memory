# 从 Nocturne Memory 搬到 Horon（写给 AI）

用户想把 Nocturne Memory 里的记忆搬进 Horon。

搬家用 [Horon 仓库](https://github.com/Dataojitori/horon)里的两个脚本：

- `memory_migration.py`：把记忆搬进 Horon。
- `seed_transitions.py`：按旧库里记忆之间的关系，给搬进来的记忆建初始推荐。

它们读哪些设置、怎么跑、会写什么，看脚本开头的说明、`--help` 和代码本身，以代码为准。旧库全程只读。

代码里看不出来的几件事：

- 搬哪份记忆由用户定。Nocturne Memory 的一个库里可以有好几个 namespace，各是一个 AI 的记忆。
- 我们建议每个 namespace 建一个 Horon 库。一个 Horon 库就是一个装记忆的容器，原来怎么分的，搬过来还怎么分；分开也不隔断什么，需要时照样可以打开另一个库取知识。
- 搬完以后，用这份记忆的 AI 要换成 Horon 的开局方式，见 Horon 的 `docs/INSTALL.md`。
