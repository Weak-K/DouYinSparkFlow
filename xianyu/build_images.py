#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成闲鱼商品图（HTML -> headless Chrome 截图），不依赖任何图片生成额度。"""

import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "img")
TMP = os.path.join(ROOT, ".html")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

os.makedirs(OUT, exist_ok=True)
os.makedirs(TMP, exist_ok=True)

BASE_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
  background:#fff;
}
.page { position:relative; overflow:hidden; }
"""


def page(w, h, cls, css, body):
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        + BASE_CSS
        + "body{width:%dpx;height:%dpx;} .page{width:%dpx;height:%dpx;}%s" % (w, h, w, h, css)
        + "</style></head><body><div class='page %s'>" % cls
        + body
        + "</div></body></html>"
    )


# ----------------------------------------------------------------------------
# 1. 封面
# ----------------------------------------------------------------------------
P1_CSS = """
.p1 { background:linear-gradient(158deg,#FF3355 0%,#FF6B35 52%,#FFB627 100%);
      padding:78px 76px; display:flex; flex-direction:column; }
.blob { position:absolute; border-radius:50%; }
.b1 { width:560px; height:560px; right:-190px; top:-170px; background:rgba(255,255,255,.13); }
.b2 { width:340px; height:340px; right:40px; bottom:-140px; background:rgba(255,255,255,.10); }
.b3 { width:180px; height:180px; left:-70px; bottom:180px; background:rgba(255,255,255,.08); }
.pill { display:inline-flex; align-items:center; gap:12px; align-self:flex-start;
        background:rgba(255,255,255,.22); border:2px solid rgba(255,255,255,.5);
        color:#fff; font-size:28px; font-weight:600; padding:14px 30px; border-radius:999px;
        letter-spacing:1px; position:relative; z-index:3; }
.dot { width:14px; height:14px; border-radius:50%; background:#fff; }
.h1 { font-size:124px; line-height:1.06; font-weight:900; color:#fff; letter-spacing:2px;
      margin-top:44px; position:relative; z-index:3; text-shadow:0 6px 26px rgba(150,20,20,.28); }
.h1 em { font-style:normal; font-size:150px; }
.sub { margin-top:30px; font-size:38px; font-weight:600; color:rgba(255,255,255,.95);
       letter-spacing:2px; position:relative; z-index:3; }
.chips { margin-top:44px; display:grid; grid-template-columns:1fr 1fr; gap:20px;
         position:relative; z-index:3; }
.chip { background:rgba(255,255,255,.94); border-radius:20px; padding:26px 28px;
        font-size:31px; font-weight:800; color:#C2183B; letter-spacing:1px;
        box-shadow:0 10px 26px rgba(140,30,20,.16); display:flex; align-items:center; gap:16px; }
.chip i { font-style:normal; font-size:34px; }
.bottom { margin-top:auto; display:flex; align-items:center; justify-content:space-between;
          background:#fff; border-radius:26px; padding:30px 40px; position:relative; z-index:3;
          box-shadow:0 14px 34px rgba(140,30,20,.22); }
.price { display:flex; align-items:baseline; gap:6px; }
.price .y { font-size:34px; font-weight:800; color:#FF3355; }
.price .n { font-size:74px; font-weight:900; color:#FF3355; line-height:1; letter-spacing:-2px; }
.price .q { font-size:30px; font-weight:700; color:#8A8A8A; margin-left:8px; }
.gt { text-align:right; font-size:26px; font-weight:700; color:#3D3D3D; line-height:1.6; }
.gt s { display:block; font-size:22px; color:#9A9A9A; font-weight:600; }
"""

P1_BODY = """
<div class='blob b1'></div><div class='blob b2'></div><div class='blob b3'></div>
<div class='pill'><span class='dot'></span>云端托管 · 手机不用挂机</div>
<div class='h1'>抖音火花<br><em>自动续</em></div>
<div class='sub'>到点自动发 · 断了即时提醒你</div>
<div class='chips'>
  <div class='chip'><i>👥</i>多账号统一管理</div>
  <div class='chip'><i>💬</i>多好友多条任务</div>
  <div class='chip'><i>⏰</i>每天多档定时</div>
  <div class='chip'><i>📧</i>掉线邮件提醒</div>
</div>
<div class='bottom'>
  <div class='price'><span class='y'>￥</span><span class='n'>3.9</span><span class='q'>起 · 7天体验</span></div>
  <div class='gt'>闲鱼担保交易<s>网页控制台 · 随时可查</s></div>
</div>
"""

# ----------------------------------------------------------------------------
# 2. 痛点 -> 解决
# ----------------------------------------------------------------------------
P2_CSS = """
.p2 { background:#FFFFFF; padding:58px 64px; }
.hd { font-size:54px; font-weight:900; color:#16181D; letter-spacing:1px; line-height:1.3; }
.hd u { text-decoration:none; color:#FF3355; }
.hline { width:96px; height:9px; border-radius:6px; background:#FF3355; margin-top:20px; }
.sec { margin-top:42px; font-size:29px; font-weight:900; color:#7A7F8C; letter-spacing:2px; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:22px; margin-top:20px; }
.card { border-radius:20px; padding:32px 26px; display:flex; gap:16px; align-items:flex-start; }
.pain { background:#F5F6F8; border:2px solid #E6E8EC; }
.good { background:#FFF1F3; border:2px solid #FFC9D2; }
.ico { flex:0 0 auto; width:46px; height:46px; border-radius:12px; display:flex;
       align-items:center; justify-content:center; font-size:26px; font-weight:900; color:#fff; }
.ico.x { background:#B9BEC9; }
.ico.v { background:#FF3355; }
.tx { font-size:28px; font-weight:700; color:#22252C; line-height:1.45; }
.tx s { display:block; font-size:23px; font-weight:600; color:#8B909C; text-decoration:none;
        margin-top:6px; line-height:1.45; }
.arrow { margin:38px 0 0; text-align:center; font-size:32px; color:#FFB627; font-weight:900;
         letter-spacing:8px; }
"""

P2_BODY = """
<div class='hd'>续火花最怕两件事：<u>忘了</u>，和<u>断了不知道</u></div>
<div class='hline'></div>

<div class='sec'>自己续 / 找便宜代挂</div>
<div class='cols'>
  <div class='card pain'><div class='ico x'>✕</div><div class='tx'>一天忙忘了<s>几百天的火花，一次清零</s></div></div>
  <div class='card pain'><div class='ico x'>✕</div><div class='tx'>手机得一直挂着<s>切号、掉后台、耗电发热</s></div></div>
  <div class='card pain'><div class='ico x'>✕</div><div class='tx'>黑盒代挂断了没人说<s>等你发现，火已经灭了</s></div></div>
  <div class='card pain'><div class='ico x'>✕</div><div class='tx'>多个号顾不过来<s>手动切换，天天提心吊胆</s></div></div>
</div>

<div class='arrow'>▼ ▼ ▼</div>

<div class='sec'>我们的做法</div>
<div class='cols'>
  <div class='card good'><div class='ico v'>✓</div><div class='tx'>到点自动发<s>电脑手机关机都不影响</s></div></div>
  <div class='card good'><div class='ico v'>✓</div><div class='tx'>网页控制台自己看<s>任务、内容、时间随你改</s></div></div>
  <div class='card good'><div class='ico v'>✓</div><div class='tx'>掉线立刻邮件通知<s>第一时间补扫码，不断火</s></div></div>
  <div class='card good'><div class='ico v'>✓</div><div class='tx'>多号多好友一次配好<s>一个页面管到底</s></div></div>
</div>
"""

# ----------------------------------------------------------------------------
# 3. 功能 / 控制台
# ----------------------------------------------------------------------------
P3_CSS = """
.p3 { background:#12141A; padding:70px 70px; color:#fff; }
.p3 .b { position:absolute; width:700px; height:700px; border-radius:50%;
         background:radial-gradient(circle,rgba(255,51,85,.30),transparent 70%);
         right:-240px; top:-240px; }
.k { font-size:30px; font-weight:800; color:#FF3355; letter-spacing:6px; }
.hd3 { font-size:70px; font-weight:900; margin-top:18px; letter-spacing:2px; }
.hd3 em { font-style:normal; color:#25F4EE; }
.mock { margin-top:44px; background:#1B1E26; border:2px solid #2C313D; border-radius:22px;
        overflow:hidden; position:relative; z-index:2; }
.mbar { height:52px; background:#22262F; display:flex; align-items:center; gap:10px; padding:0 22px; }
.mbar i { display:block; width:14px; height:14px; border-radius:50%; flex:0 0 auto; }
.mbar .r { background:#FF5F57; } .mbar .y { background:#FEBC2E; } .mbar .g { background:#28C840; }
.mbar .u { margin-left:16px; font-size:20px; color:#6C7383; letter-spacing:1px; white-space:nowrap; }
.mrow { display:flex; align-items:center; padding:20px 24px; border-bottom:1px solid #262B36;
        font-size:24px; }
.mrow:last-child { border-bottom:none; }
.mrow .nm { width:280px; color:#EDEFF4; font-weight:700; }
.mrow .ms { flex:1; color:#8A91A0; }
.tag { font-size:21px; font-weight:800; padding:6px 18px; border-radius:999px; }
.ok { background:rgba(37,244,238,.14); color:#25F4EE; }
.wait { background:rgba(255,198,39,.16); color:#FFC627; }
.warn { background:rgba(255,51,85,.16); color:#FF6B85; }
.feat { margin-top:40px; display:grid; grid-template-columns:1fr 1fr 1fr; gap:22px;
        position:relative; z-index:2; }
.f { background:#1B1E26; border:2px solid #2C313D; border-radius:18px; padding:24px 22px; }
.f b { display:block; font-size:28px; font-weight:900; color:#fff; }
.f s { display:block; font-size:22px; color:#878E9D; text-decoration:none; margin-top:10px;
       line-height:1.5; }
"""

P3_BODY = """
<div class='b'></div>
<div class='k'>WEB 控制台</div>
<div class='hd3'>你随时能看到的<em>执行记录</em></div>
<div class='mock'>
  <div class='mbar'><i class='r'></i><i class='y'></i><i class='g'></i>
    <span class='u'>火花控制台 / 执行记录</span></div>
  <div class='mrow'><div class='nm'>好友A · 09:00</div><div class='ms'>已发送 · 用时 12s</div><div class='tag ok'>成功</div></div>
  <div class='mrow'><div class='nm'>好友B · 10:00</div><div class='ms'>已发送 · 用时 15s</div><div class='tag ok'>成功</div></div>
  <div class='mrow'><div class='nm'>好友C · 11:00</div><div class='ms'>已安排 1 分钟后自动重试</div><div class='tag wait'>重试中</div></div>
  <div class='mrow'><div class='nm'>账号2 · 09:00</div><div class='ms'>登录状态失效 · 已发邮件提醒</div><div class='tag warn'>需重登</div></div>
</div>
<div class='feat'>
  <div class='f'><b>多账号隔离</b><s>每个号各管各的，互不影响</s></div>
  <div class='f'><b>多好友多任务</b><s>一个号同时续多个好友</s></div>
  <div class='f'><b>每天多档时间</b><s>不是只碰运气发一次</s></div>
  <div class='f'><b>消息自定义</b><s>内容你自己写，不用跟别人重复</s></div>
  <div class='f'><b>失败自动重试</b><s>网络抖动不用你管</s></div>
  <div class='f'><b>真实浏览器登录</b><s>不逆向接口，扫码即用</s></div>
</div>
"""

# ----------------------------------------------------------------------------
# 4. 价格
# ----------------------------------------------------------------------------
P4_CSS = """
.p4 { background:#FFFFFF; padding:56px 62px; }
.hd4 { font-size:54px; font-weight:900; color:#16181D; letter-spacing:1px; }
.hd4 u { text-decoration:none; color:#FF3355; }
.hline { width:96px; height:9px; border-radius:6px; background:#FF3355; margin-top:18px; }
table { width:100%; border-collapse:separate; border-spacing:0 9px; margin-top:22px; }
th { font-size:24px; color:#8B909C; font-weight:800; text-align:left; padding:0 24px 0;
     letter-spacing:2px; }
td { background:#F6F7F9; padding:18px 24px; font-size:28px; font-weight:800; color:#22252C;
     border-top:2px solid #EBEDF1; border-bottom:2px solid #EBEDF1; vertical-align:middle; }
td:first-child { border-left:2px solid #EBEDF1; border-radius:16px 0 0 16px; }
td:last-child  { border-right:2px solid #EBEDF1; border-radius:0 16px 16px 0; text-align:right; }
tr.hot td { background:#FFF1F3; border-color:#FFC9D2; }
tr.hot td:first-child { color:#C2183B; }
.pr { font-size:34px; font-weight:900; color:#FF3355; letter-spacing:-1px; }
.pr small { font-size:22px; font-weight:800; }
.unit { font-size:23px; color:#8B909C; font-weight:700; }
.badge { display:inline-block; font-size:19px; font-weight:900; color:#fff; background:#FF3355;
         border-radius:8px; padding:3px 11px; margin-left:10px; vertical-align:middle; }
.foot { margin-top:22px; background:#FFF8E1; border:2px solid #FFE08A; border-radius:16px;
        padding:22px 26px; font-size:24px; font-weight:700; color:#7A5A00; line-height:1.6; }
"""

P4_BODY = """
<div class='hd4'>价格简单，<u>买越久越便宜</u></div>
<div class='hline'></div>
<table>
  <tr><th>套餐</th><th></th><th style='text-align:right'>价格</th></tr>
  <tr><td>7天体验 · 1个号<span class='badge'>先试</span></td><td class='unit'>先跑通再加量</td><td class='pr'><small>￥</small>3.9</td></tr>
  <tr class='hot'><td>月卡 · 1个号<span class='badge'>热销</span></td><td class='unit'>约 0.33 元/天</td><td class='pr'><small>￥</small>9.9</td></tr>
  <tr><td>季卡 · 1个号</td><td class='unit'>约 0.29 元/天</td><td class='pr'><small>￥</small>25.9</td></tr>
  <tr><td>年卡 · 1个号</td><td class='unit'>约 0.19 元/天</td><td class='pr'><small>￥</small>69.9</td></tr>
  <tr><td>永久卡 · 1个号</td><td class='unit'>一次买断</td><td class='pr'><small>￥</small>128</td></tr>
  <tr><td>3个号 · 月卡</td><td class='unit'>约 8.3 元/号</td><td class='pr'><small>￥</small>24.9</td></tr>
  <tr><td>5个号 · 月卡</td><td class='unit'>约 8.0 元/号</td><td class='pr'><small>￥</small>39.9</td></tr>
  <tr><td>源码授权 · 自己部署</td><td class='unit'>含部署文档 + 答疑</td><td class='pr'><small>￥</small>299<small>起</small></td></tr>
</table>
<div class='foot'>只按「号」算钱：一个号含 5 个好友、每天 2 档时间；超出每好友 +2 元/月。<br>
套餐到期前会提醒你，不自动扣费。想加号随时补差价。</div>
"""

# ----------------------------------------------------------------------------
# 5. 流程
# ----------------------------------------------------------------------------
P5_CSS = """
.p5 { background:linear-gradient(170deg,#FFF9E6 0%,#FFFFFF 60%); padding:56px 62px; }
.blob5 { position:absolute; width:520px; height:520px; border-radius:50%;
         background:rgba(255,214,0,.22); right:-180px; top:-180px; }
.hd5 { font-size:54px; font-weight:900; color:#16181D; position:relative; z-index:2; }
.hd5 u { text-decoration:none; color:#FF3355; }
.hline { width:96px; height:9px; border-radius:6px; background:#FFB627; margin-top:18px; }
.steps { margin-top:42px; display:flex; flex-direction:column; gap:24px; position:relative; z-index:2; }
.st { display:flex; gap:24px; align-items:flex-start; background:#fff; border-radius:20px;
      padding:32px 30px; border:2px solid #F0F1F4; box-shadow:0 8px 22px rgba(30,40,60,.06); }
.no { flex:0 0 auto; width:62px; height:62px; border-radius:16px; background:#FF3355; color:#fff;
      font-size:32px; font-weight:900; display:flex; align-items:center; justify-content:center; }
.st b { display:block; font-size:32px; font-weight:900; color:#16181D; }
.st s { display:block; font-size:25px; color:#7A7F8C; text-decoration:none; margin-top:10px;
        line-height:1.5; font-weight:600; }
.note { margin-top:34px; background:#FFF1F3; border:2px solid #FFC9D2; border-radius:18px;
        padding:26px 30px; font-size:25px; font-weight:700; color:#B31734; line-height:1.7;
        position:relative; z-index:2; }
"""

P5_BODY = """
<div class='blob5'></div>
<div class='hd5'>怎么买？<u>四步搞定</u></div>
<div class='hline'></div>
<div class='steps'>
  <div class='st'><div class='no'>1</div><div><b>拍下对应套餐</b>
    <s>先买 7 天体验卡试，跑通再加量，稳妥不踩坑</s></div></div>
  <div class='st'><div class='no'>2</div><div><b>私聊告诉我三件事</b>
    <s>要续哪几个好友 · 每天几点发 · 想发什么内容（没想法我帮你写）</s></div></div>
  <div class='st'><div class='no'>3</div><div><b>扫码授权，不用给密码</b>
    <s>我发你一个登录二维码，用抖音 App 扫一下就完成绑定，全程不接触你的账号密码</s></div></div>
  <div class='st'><div class='no'>4</div><div><b>配好开跑，你随时能查</b>
    <s>任务列表和执行记录在网页控制台里，成没成、几点发的，一目了然</s></div></div>
</div>
<div class='note'>全程走闲鱼担保交易，别私下转账。<br>
数字服务，开始服务后不支持退款，请先买体验卡确认效果。</div>
"""

# ----------------------------------------------------------------------------
# 6. 详情长图
# ----------------------------------------------------------------------------
P6_CSS = """
.p6 { background:#FFFFFF; padding:0 0 90px; }
.hero { background:linear-gradient(150deg,#FF3355,#FF6B35 70%,#FFB627); padding:78px 70px 66px; }
.hero .pill { display:inline-block; background:rgba(255,255,255,.22); border:2px solid rgba(255,255,255,.5);
  color:#fff; font-size:26px; font-weight:800; padding:10px 26px; border-radius:999px; letter-spacing:2px; }
.hero h1 { font-size:88px; font-weight:900; color:#fff; line-height:1.15; margin-top:26px; letter-spacing:1px; }
.hero p { font-size:32px; font-weight:700; color:rgba(255,255,255,.95); margin-top:22px; line-height:1.6; }
.wrap { padding:64px 70px 0; }
.sec6 { display:flex; align-items:center; gap:18px; font-size:46px; font-weight:900; color:#16181D;
        margin-top:64px; letter-spacing:1px; }
.sec6:first-child { margin-top:0; }
.sec6 i { font-style:normal; width:12px; height:42px; border-radius:6px; background:#FF3355; display:block; }
.ul { margin-top:30px; display:flex; flex-direction:column; gap:22px; }
.li { display:flex; gap:18px; font-size:31px; line-height:1.62; color:#2A2E38; font-weight:600; }
.li em { font-style:normal; color:#FF3355; font-weight:900; flex:0 0 auto; }
.li b { color:#16181D; font-weight:900; }
.pricebox { margin-top:30px; border:3px solid #FFC9D2; background:#FFF7F8; border-radius:24px;
            padding:34px 36px; }
.prow { display:flex; justify-content:space-between; align-items:center; padding:18px 0;
        border-bottom:2px dashed #FFD8DE; font-size:32px; font-weight:800; color:#2A2E38; }
.prow:last-child { border-bottom:none; }
.prow span:last-child { color:#FF3355; font-size:36px; font-weight:900; }
.warnbox { margin-top:30px; background:#F6F7F9; border-radius:20px; padding:32px 36px;
           font-size:28px; line-height:1.75; color:#5A6070; font-weight:600; }
.warnbox b { color:#16181D; }
.tagline { margin-top:56px; text-align:center; font-size:40px; font-weight:900; color:#16181D; }
.tagline u { text-decoration:none; color:#FF3355; }
"""

P6_BODY = """
<div class='hero'>
  <div class='pill'>云端托管 · 不用挂机</div>
  <h1>抖音火花自动续<br>多账号 · 多好友 · 掉线提醒</h1>
  <p>到点自动发，不用开手机不用挂电脑。<br>登录状态一失效，立刻发邮件通知你，不让火花在你不知情的时候断掉。</p>
</div>
<div class='wrap'>

  <div class='sec6'><i></i>为什么选我们</div>
  <div class='ul'>
    <div class='li'><em>·</em><div>手动续火花最怕两件事：<b>忘了</b>，和<b>断了不知道</b>。便宜的代挂大多是黑盒，断了没人告诉你，等你发现火已经灭了。</div></div>
    <div class='li'><em>·</em><div>我们做的是<b>「自动续 + 主动提醒」</b>：任务到点自己跑，登录态一旦失效立刻发邮件到你邮箱，你第一时间补扫码就行。</div></div>
  </div>

  <div class='sec6'><i></i>能做什么</div>
  <div class='ul'>
    <div class='li'><em>·</em><div><b>多账号管理</b>：一个控制台管多个抖音号，账号数据互相隔离，互不干扰。</div></div>
    <div class='li'><em>·</em><div><b>多好友多任务</b>：一个号可以同时给多个好友续，每个好友单独配消息和时间（标准套餐含 5 个好友）。</div></div>
    <div class='li'><em>·</em><div><b>每天多档定时</b>：可以设 9 点 / 10 点 / 11 点多个时间点，不是只碰运气发一次。</div></div>
    <div class='li'><em>·</em><div><b>消息内容自定义</b>：每条发什么你自己定，不用跟别人用一样的话术。</div></div>
    <div class='li'><em>·</em><div><b>失败自动重试</b>：网络抖动导致的失败会自动再试，不用你操心。</div></div>
    <div class='li'><em>·</em><div><b>执行记录可查</b>：每次跑没跑、几点跑的、成没成，网页控制台里全都留痕。</div></div>
    <div class='li'><em>·</em><div><b>真实浏览器登录</b>：模拟真人在网页版操作，不是逆向接口，扫码即用，不需要你把密码给我。</div></div>
  </div>

  <div class='sec6'><i></i>价格</div>
  <div class='pricebox'>
    <div class='prow'><span>7天体验 · 1个号</span><span>￥3.9</span></div>
    <div class='prow'><span>月卡 · 1个号</span><span>￥9.9</span></div>
    <div class='prow'><span>季卡 · 1个号</span><span>￥25.9</span></div>
    <div class='prow'><span>年卡 · 1个号</span><span>￥69.9</span></div>
    <div class='prow'><span>永久卡 · 1个号</span><span>￥128</span></div>
    <div class='prow'><span>3个号 · 月卡</span><span>￥24.9</span></div>
    <div class='prow'><span>5个号 · 月卡</span><span>￥39.9</span></div>
    <div class='prow'><span>源码授权 · 自己部署</span><span>￥299 起</span></div>
  </div>
  <div class='ul'>
    <div class='li'><em>·</em><div>只按「号」算钱，<b>一个号含 5 个好友、每天 2 档时间</b>，超出每好友 +2 元/月。</div></div>
    <div class='li'><em>·</em><div>套餐到期前会提醒，<b>不自动扣费</b>；中途想加号，补差价即可。</div></div>
  </div>

  <div class='sec6'><i></i>购买流程</div>
  <div class='ul'>
    <div class='li'><em>1</em><div>拍下对应套餐（建议先买 7 天体验卡）。</div></div>
    <div class='li'><em>2</em><div>私聊告诉我：要续哪几个好友、每天几点发、想发什么内容。</div></div>
    <div class='li'><em>3</em><div>我发你登录二维码，用抖音 App 扫码授权，<b>不需要提供密码</b>。</div></div>
    <div class='li'><em>4</em><div>配好开跑，之后在网页控制台随时查任务和执行记录。</div></div>
  </div>

  <div class='sec6'><i></i>购买须知</div>
  <div class='warnbox'>
    <b>1.</b> 全程走闲鱼担保交易，请不要私下转账，脱离平台没有保障。<br>
    <b>2.</b> 数字服务，开始服务后不支持退款，请先买体验卡确认效果。<br>
    <b>3.</b> 请勿用于骚扰、批量营销等用途；仅限个人正常好友续火花。<br>
    <b>4.</b> 任何自动化方式都存在平台风控风险，介意请勿拍，建议不要用最看重的主号。<br>
    <b>5.</b> 页面结构更新可能造成短暂波动，期间由我们负责处理，不影响服务时长。
  </div>

  <div class='tagline'>有疑问先私聊，<u>随时在线</u></div>
</div>
"""

PAGES = [
    ("01_封面", 1200, 1200, "p1", P1_CSS, P1_BODY, False),
    ("02_痛点对比", 1200, 1200, "p2", P2_CSS, P2_BODY, False),
    ("03_功能控制台", 1200, 1200, "p3", P3_CSS, P3_BODY, False),
    ("04_价格表", 1200, 1200, "p4", P4_CSS, P4_BODY, False),
    ("05_购买流程", 1200, 1200, "p5", P5_CSS, P5_BODY, False),
    ("06_详情长图", 1080, 5200, "p6", P6_CSS, P6_BODY, True),
]


def trim_bottom(png_path, margin=80):
    """裁掉长图底部多余留白。"""
    from PIL import Image

    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    px = im.load()
    step = 4
    last = 0
    for y in range(h - 1, -1, -step):
        row_blank = True
        for x in range(0, w, 6):
            if px[x, y] != (255, 255, 255):
                row_blank = False
                break
        if not row_blank:
            last = y
            break
    new_h = min(h, last + margin)
    if new_h < 200:
        return
    im.crop((0, 0, w, new_h)).save(png_path)
    print("   trimmed -> %dx%d" % (w, new_h))


def main():
    for name, w, h, cls, css, body, trim in PAGES:
        html_path = os.path.join(TMP, name + ".html")
        png_path = os.path.join(OUT, name + ".png")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page(w, h, cls, css, body))
        cmd = [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--default-background-color=ffffff",
            "--window-size=%d,%d" % (w, h),
            "--screenshot=" + png_path,
            "file://" + html_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        ok = os.path.exists(png_path)
        print("%-16s %sx%s -> %s" % (name, w, h, "OK" if ok else "FAIL"))
        if ok and trim:
            trim_bottom(png_path)
        if not ok:
            print(r.stderr[-800:])


if __name__ == "__main__":
    main()
