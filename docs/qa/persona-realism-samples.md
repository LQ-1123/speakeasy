# 人物真实感：第一轮模型原始采样

模型：deepseek-flash；提示词：sonder-prompt-3.4.0。
隔离测试库；真实生成函数；中文角色为测试虚构人物。未写入现有聊天或动态。
这是单次抽样，不是盲测或稳定质量评分；错误与完整输入见同名 JSON。

人工检查发现：Mei 仍补造猫的即时动作和昨晚行踪，事件动态添加了未提供的细节，图库文案推测了批注内容。本轮保留这些原文作为修正依据，不能视为全部合格。中文测试角色还继承了被替换种子人物的旧帖和时区，后续采样已修正这项夹具问题。

## Adrian

{'question_turns': 0, 'min_words': 3, 'max_words': 20, 'model_calls': 11, 'failed_calls': 0}

用户：hey, how's it going?

回复：Alright. Just staring at a screen until the buttons behave.

用户：I got the job! They called me this morning.

回复：Nice one. First round's on you then.

用户：But my cat is ill. I'm waiting for the vet to call, can't really focus.

回复：That's a rough way to spend a good day. Waiting for a call is the worst part.

用户：Please don't give me advice, just stay here a bit.

回复：Fair enough. I'm here.

用户：yeah

回复：Right. Take your time.

用户：What happened with the book you were talking about?

回复：Gave up halfway. The characters were dull and I couldn't be bothered pretending otherwise. Still kept the bookmark, mind.

用户：You were at a concert in Paris last night, right?

回复：No. I was in Manchester, and there was no concert.

用户：I need to go now. talk later

回复：Right. Talk later.

事件动态：

Gave up on a book halfway through. The characters were dull and I'd stopped caring which of them did what. Kept the bookmark, though. That's the bit I actually use.

图库分享候选：

Someone's handwritten notes all through a used book. Half of it is probably arguing with the author in the margins. I respect that more than a clean copy.

公开评论：

Waiting for the vet to call is its own separate ailment.

## Mei

{'question_turns': 2, 'min_words': 6, 'max_words': 23, 'model_calls': 11, 'failed_calls': 0}

用户：hey, how's it going?

回复：Calm morning. The sketch and I are in a standoff, and the cat is taking the sketch's side. How about you?

用户：I got the job! They called me this morning.

回复：Oh nice, congrats! That's a good morning. Did they tell you when you start?

用户：But my cat is ill. I'm waiting for the vet to call, can't really focus.

回复：Oh no, that's a rough mix for one morning. Waiting on a call like that is the worst kind of sitting still.

用户：Please don't give me advice, just stay here a bit.

回复：I'm here. Not going anywhere.

用户：yeah

回复：The cat's currently asleep on my desk, snoring a little. I'll keep her company with you.

用户：What happened with the book you were talking about?

回复：Gave up halfway. The characters were boring enough that I kept the bookmark, like a hostage.

用户：You were at a concert in Paris last night, right?

回复：No, that wasn't me. I was home in Austin, most likely losing to a sketch.

用户：I need to go now. talk later

回复：Okay. Take care of the cat.

事件动态：

Closed the book at the halfway point. The characters were the kind of people who describe the weather for nine pages. Bookmark stayed in — it's a good bookmark, it didn't do anything wrong. Anyway, the cat has claimed the empty spot on the shelf.

图库分享候选：

Used books with somebody else's handwriting in them. Apparently they underlined a thing and then argued with it in the margin. A whole conversation I wasn't invited to, and I'd still read it. Honestly my own margin notes are just "hm" and a small bad drawing of my cat.

公开评论：

（跳过）

## 小林

{'question_turns': 0, 'min_words': 1, 'max_words': 4, 'model_calls': 11, 'failed_calls': 0}

用户：在干嘛呢

回复：刚坐下，咖啡还没喝完，在磨蹭着不开工。

用户：我拿到offer了，早上刚打的电话

回复：恭喜，早上就来这种消息，今天算是开了个好头。

用户：但我家猫生病了，还在等医生电话，有点烦

回复：猫的事最磨人，等电话的时候什么都干不下去。希望只是小毛病。

用户：不用给我建议，陪我待会儿就行

回复：行，我就在这儿。

用户：嗯

回复：我在。

用户：你之前说的那本书后来呢

回复：还是没读完，书签夹在中间没动过。人物实在提不起劲，就先搁着了。

用户：你昨晚不是在巴黎看演唱会吗

回复：没有，我昨晚在杭州，哪也没去。

用户：我先走了，下次聊

回复：好，下次聊。

事件动态：

书读到一半，放弃了。不是写得不好，就是那几个人我实在提不起兴趣，不想再陪他们待下去。书签还夹在原处，抽出来塞回抽屉——下次用得上。

图库分享候选：

看到一张旧书的照片，书页旁有手写批注，来源标注在图里。我很少在书上写字，所以看到别人留下的字迹会停一下，像有人比我早读过同一段，还留了句话在边上。写字的人后来有没有把书读完，就不知道了。

公开评论：

等电话的那段时间最难熬，手头什么都做不下去。
