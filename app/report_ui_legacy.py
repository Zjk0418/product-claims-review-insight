"""可视化报告：采集概况、综合判断、宣传落差与宣传外问题。

沿用原有独立离线报告入口，按阅读顺序组织两类分析。
概率范围与原始证据保留，计算详情按需打开。
占位符：__REPORT_DATA__ / __REPORT_TITLE__ / __VIEW_MODE__。
"""
from __future__ import annotations

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>__REPORT_TITLE__</title>
<style>
:root{
  --ink:#0b1220;--muted:#63718a;--line:#dce5f2;--bg:#f3f7fd;--panel:#ffffff;
  --brand:#066bff;--brand-soft:#edf4ff;--brand-gradient:linear-gradient(135deg,#2990ff 0%,#066bff 70%,#0057e7 100%);
  --high:#d93f3f;--high-soft:#fdeaea;--high-ink:#a02323;
  --medium:#d98500;--medium-soft:#fff4e0;--medium-ink:#8a5400;
  --low:#159765;--low-soft:#edf8f3;--low-ink:#116447;
  --insufficient:#6f8296;--insufficient-soft:#eef3f8;--insufficient-ink:#4d6073;
  --none:#8a97a6;--none-soft:#f1f4f7;--none-ink:#5f6d7c;
  --violet:#487bd4;--violet-soft:#edf3ff;
  --shadow:0 1px 2px rgba(15,23,42,.04),0 14px 32px -24px rgba(17,39,76,.22);
  --radius:16px;--evidence-page-glow:#eaf2ff;--evidence-page-bg:#f3f7fd;
}
*{box-sizing:border-box}
html,body{min-height:100%}
body{
  margin:0;font:15px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  color:var(--ink);overflow:auto;-webkit-font-smoothing:antialiased;
  background:radial-gradient(960px 420px at 100% -15%,#e3edff 0%,transparent 64%),radial-gradient(1100px 460px at 12% -8%,var(--evidence-page-glow) 0%,transparent 62%),var(--evidence-page-bg);
  transition:background-color .35s ease;
}
[hidden]{display:none!important}
button,input,select{font:inherit;color:inherit}
button,select{cursor:pointer}
button{border:1px solid #cadbf5;background:#f5f8fd;color:#065cdb;border-radius:10px;padding:7px 12px;font-weight:600;transition:border-color .18s,background .18s,transform .18s,box-shadow .18s}
button:hover{border-color:#8bb6fa;background:#edf4ff}
button:active{transform:translateY(1px)}
button:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #a9caff;outline-offset:2px}
h1,h2,h3{margin:0;color:var(--ink);font-weight:750}
.shell{max-width:1500px;margin:0 auto;padding:22px 26px 24px;min-height:100vh;display:flex;flex-direction:column;gap:14px}

/* ---------- 顶部 ---------- */
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;padding:21px 23px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(118deg,#fff 62%,#f2f7ff);box-shadow:var(--shadow)}
.eyebrow{font-size:10.5px;letter-spacing:1.8px;font-weight:750;color:var(--brand);text-transform:uppercase}
.title{font-size:27px;line-height:1.32;font-weight:780;margin:4px 0 3px;max-width:960px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#0b1220}
.subtitle{font-size:12.5px;color:var(--muted)}
.header-actions{display:flex;gap:8px;flex-shrink:0;padding-top:4px}
.header-actions button{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:650;padding:9px 14px;border-color:#066bff;border-radius:10px;background:var(--brand-gradient);color:#fff;box-shadow:0 4px 10px rgba(22,119,255,.12)}
.header-actions button:hover{border-color:#0057e7;background:linear-gradient(135deg,#1679ff,#0057e7);box-shadow:0 5px 12px rgba(22,119,255,.18)}
.header-actions #methodButton{background:#fff;border-color:#cadbf5;color:var(--brand);box-shadow:0 1px 2px rgba(15,23,42,.025)}
.header-actions #methodButton:hover{background:var(--brand-soft);border-color:#8bb6fa}
.header-actions svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}

.notice{padding:9px 14px;border-radius:10px;background:#f3f6fb;color:#4a5a72;font-size:12px;border:1px solid #dce5f2}

/* ---------- 指标卡 ---------- */
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.metric{--tone:var(--brand);--tone-soft:var(--brand-soft);display:flex;align-items:center;gap:13px;
  padding:14px 16px;border:1px solid var(--line);border-radius:16px;background:#fff;box-shadow:var(--shadow);
  animation:rise .5s cubic-bezier(.2,.8,.2,1) both}
.metric .icon{width:40px;height:40px;border:1px solid #dce8fc;border-radius:11px;display:grid;place-items:center;background:var(--tone-soft);color:var(--tone);flex:none}
.metric .icon svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.metric .meta{display:flex;flex-direction:column;gap:1px;min-width:0}
.metric .label{font-size:12px;color:#4a5a72;font-weight:600}
.metric b{font-size:31px;font-weight:750;letter-spacing:-.8px;line-height:1.15;font-variant-numeric:tabular-nums}
.metric small{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---------- 工作区 ---------- */
.workspace{display:grid;grid-template-columns:minmax(318px,.82fr) minmax(520px,1.68fr);gap:14px;align-items:start;flex:none}
.side{display:flex;flex-direction:column;gap:14px;min-height:0;position:sticky;top:14px;align-self:start;max-height:calc(100vh - 28px);overflow:auto;padding-right:2px;scrollbar-width:thin;scrollbar-color:#c9d6df transparent}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);display:flex;flex-direction:column;min-height:0;overflow:hidden;box-shadow:var(--shadow)}
/* 左栏在 sticky 容器里有高度上限，面板必须保持自身高度，超出交给 .side 滚动，
   否则 flex 会把面板压扁、overflow:hidden 再把圆环裁掉。 */
.side>.panel{flex:0 0 auto}
.panel.grow{flex:none}
.panel-head{padding:16px 18px 13px;border-bottom:1px solid var(--line);background:#fff}
.panel-head h2{display:flex;align-items:center;gap:8px;font-size:17px;letter-spacing:-.2px;font-weight:750}
.panel-head h2:before{content:"";width:3px;height:15px;border-radius:3px;background:var(--brand);flex:none}
.panel-head .desc{font-size:12px;color:var(--muted);margin-top:5px}
.head-split{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.inline-button{font-size:12px;padding:5px 10px;border-radius:10px;color:#065cdb;background:#edf4ff;white-space:nowrap;flex:none}
.scroll{overflow:auto;scrollbar-width:thin;scrollbar-color:#c9d6df transparent;overscroll-behavior:contain}
.panel-foot{padding:9px 15px 12px;font-size:11.5px;line-height:1.55;color:var(--muted);border-top:1px solid #e6ebf2}

/* ---------- 圆环 ---------- */
.rings{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;padding:14px 10px 8px}
.rings.one{grid-template-columns:minmax(0,1fr)}
.rings.two{grid-template-columns:repeat(2,minmax(0,1fr))}
.ring-card{--tone:var(--none);display:flex;flex-direction:column;align-items:center;gap:6px;padding:4px 0 2px;
  opacity:0;transform:translateY(7px);animation:rise .55s cubic-bezier(.2,.8,.2,1) forwards}
.ring-visual{position:relative;width:min(108px,100%);aspect-ratio:1/1}
.ring-visual svg{width:100%;height:100%;transform:rotate(-90deg);display:block}
.ring-track{fill:none;stroke:#edf1f5;stroke-width:10}
.ring-fill{fill:none;stroke:var(--tone);stroke-width:10;stroke-linecap:round;transition:stroke-dashoffset 1.1s cubic-bezier(.22,.9,.28,1)}
.ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0}
.ring-center strong{font-size:21px;font-weight:680;letter-spacing:-.5px;font-variant-numeric:tabular-nums;color:var(--tone)}
.ring-center span{font-size:11px;color:var(--muted);margin-top:1px}
.ring-foot{font-size:11.5px;color:var(--muted);text-align:center;line-height:1.45}
.ring-aspects{display:flex;flex-direction:column;gap:1px;padding:2px 12px 6px;border-top:1px solid #e6ebf2;margin-top:4px}
.ra-title{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;color:var(--muted);padding:8px 3px 3px}
.ra-title b{color:var(--ink);font-size:13px;font-weight:650}
.ring-aspect{--tone:var(--high);display:flex;align-items:center;gap:8px;width:100%;border:0;background:transparent;border-radius:8px;padding:5px 7px;text-align:left;transition:background .18s}
.ring-aspect:hover,.ring-aspect:focus-visible{background:#f4f7fb}
.ra-dot{width:7px;height:7px;border-radius:50%;background:var(--tone);flex:none}
.ra-name{flex:1;min-width:0;font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ra-rate{font-size:13.5px;font-weight:680;color:var(--tone);font-variant-numeric:tabular-nums}
.ra-num{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;min-width:56px;text-align:right}
.ra-more{font-size:11px;color:var(--muted);padding:4px 7px 0}
.empty{padding:26px 18px;text-align:center;font-size:12px;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:13px;margin-bottom:3px}

/* ---------- 方面条 ---------- */
.bars{padding:7px 10px 12px;display:flex;flex-direction:column;gap:1px}
.issue-panel{margin-top:14px}
.issue-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:12px 16px 16px}
.issue-row{display:flex;flex-direction:column;gap:5px;padding:10px 12px;border:1px solid #e6edf3;border-radius:11px;background:#fafcff;cursor:pointer;transition:border-color .18s,background .18s}
.issue-row:hover{border-color:#aac7f3;background:#edf4ff}
.issue-row:focus-visible{outline:3px solid #a9caff;outline-offset:2px}
.issue-top{display:flex;align-items:center;gap:8px;justify-content:space-between}
.issue-name{font-weight:600;font-size:13.5px;color:#152238;line-height:1.35}
.issue-meta{font-size:11.5px;color:#63718a}
.issue-quote{font-size:12px;color:#4a5a72;line-height:1.55;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(max-width:900px){.issue-list{grid-template-columns:1fr}}
.bar-row{--tone:var(--low);display:block;width:100%;text-align:left;border:0;background:transparent;border-radius:11px;padding:9px 10px;transition:background .18s}
.bar-row:hover{background:#f4f7fc}
.bar-row.active{background:var(--brand-soft)}
.bar-top{display:flex;align-items:center;gap:8px}
.bar-name{flex:1;min-width:0;font-size:14.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-value{font-size:14.5px;font-weight:680;color:var(--tone);font-variant-numeric:tabular-nums}
.track{height:7px;border-radius:99px;background:#e6ebf2;overflow:hidden;margin:6px 0 4px}
.fill{height:100%;width:0;border-radius:99px;background:var(--tone);transition:width .9s cubic-bezier(.22,.9,.28,1)}
.bar-foot{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;color:var(--muted)}
.bar-foot .lvl{color:var(--tone);font-weight:600}

/* ---------- 工具栏 ---------- */
.toolbar{display:grid;grid-template-columns:minmax(140px,1.5fr) repeat(3,minmax(96px,1fr));gap:8px;padding:10px 16px;border-bottom:1px solid var(--line);background:#f5f7fb}
.toolbar input,.toolbar select{border:1px solid #d4dfed;border-radius:9px;padding:8px 10px;background:#fff;font-size:12px;min-width:0;box-shadow:0 1px 2px rgba(15,23,42,.025)}
.toolbar input::placeholder{color:#8795a8}

/* ---------- 方面卡片 ---------- */
.cards{padding:11px 14px 24px;display:flex;flex-direction:column;gap:10px;overflow:visible}
.aspect-card{--tone:var(--none);--tone-soft:var(--none-soft);--tone-ink:var(--none-ink);border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden;
  animation:rise .45s cubic-bezier(.2,.8,.2,1) both}
.aspect-card[open]{border-color:#c7d6ea;box-shadow:0 3px 12px rgba(15,23,42,.045)}
.aspect-card>summary{display:flex;align-items:center;gap:11px;padding:11px 13px;cursor:pointer;list-style:none}
.aspect-card>summary::-webkit-details-marker{display:none}
.aspect-card>summary:hover{background:#fafcff}
.idx{width:26px;height:26px;border-radius:50%;background:var(--tone);color:#fff;font-size:12px;font-weight:700;display:grid;place-items:center;flex:none;font-variant-numeric:tabular-nums}
.head-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.head-text .name{font-size:15px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head-text small{font-size:11.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head-support{display:flex;flex-direction:column;align-items:flex-end;gap:0;flex:none;min-width:86px}
.head-support b{font-size:13.5px;font-weight:650;font-variant-numeric:tabular-nums}
.head-support small{font-size:11px;color:var(--muted)}
.chev{width:7px;height:7px;border-right:1.8px solid #8291a7;border-bottom:1.8px solid #8291a7;transform:rotate(45deg);transition:transform .25s;margin:0 3px 4px 2px;flex:none}
.aspect-card[open] .chev{transform:rotate(225deg);margin-bottom:-2px}
.tag{display:inline-flex;align-items:center;border-radius:99px;padding:3px 10px;font-size:11.5px;font-weight:650;white-space:nowrap;line-height:1.5}
.tag.high{background:var(--high-soft);color:var(--high-ink)}
.tag.medium{background:var(--medium-soft);color:var(--medium-ink)}
.tag.low{background:var(--low-soft);color:var(--low-ink)}
.tag.insufficient{background:var(--insufficient-soft);color:var(--insufficient-ink)}
.tag.none{background:var(--none-soft);color:var(--none-ink)}

.aspect-body{padding:2px 13px 13px;border-top:1px solid #e6ebf2}
.mini-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0 8px}
.mini{background:#f5f7fb;border:1px solid #e1e8f2;border-radius:10px;padding:6px 8px;display:flex;flex-direction:column;gap:0}
.mini small{font-size:10.5px;color:var(--muted);line-height:1.5}
.mini b{font-size:14px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.35}
.aspect-note{font-size:12px;color:var(--muted);margin:2px 0 8px}

.claim{border:1px solid #dce5f2;border-radius:12px;background:#fff;margin-top:8px;overflow:hidden}
.claim>summary{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:pointer;list-style:none;background:#f7f9fc}
.claim>summary::-webkit-details-marker{display:none}
.claim>summary:hover{background:#f4f7fb}
.claim-title{flex:1;min-width:0;font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.claim-count{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.claim-body{padding:0 11px 11px}
.quote-label{font-size:11px;color:var(--muted);margin:8px 0 3px}
.promo{margin:0;padding:9px 12px;border-left:3px solid var(--brand);background:var(--brand-soft);border-radius:0 10px 10px 0;font-size:13px;color:#244772;line-height:1.65;word-break:break-word}
.finding{font-size:12px;margin:8px 0 0;line-height:1.65;color:#152238}
.basis{font-size:10.5px;color:var(--muted);margin:5px 0 0;line-height:1.6}
.ev-head{display:flex;align-items:center;gap:7px;font-size:10.5px;color:var(--muted);margin:10px 0 5px}
.ev-head:before{content:"";width:3px;height:11px;border-radius:2px;background:#9eb8db}
.evidence{border:1px solid #dfe6f0;border-radius:10px;background:#fff;padding:10px 12px;margin-top:8px;box-shadow:0 1px 2px rgba(15,23,42,.025)}
.ev-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.stars{color:#e3a008;letter-spacing:1.5px;font-size:12px;line-height:1}
.ev-meta{font-size:10px;color:var(--muted)}
.ev-text{margin:6px 0 0;font-size:13px;line-height:1.66;word-break:break-word;color:#152238}
.empty-list{padding:34px 18px;text-align:center;color:var(--muted);font-size:12px}
.empty-list b{display:block;color:var(--ink);font-size:13px;margin-bottom:4px}

/* ---------- 底栏与弹窗 ---------- */
footer{display:flex;justify-content:space-between;gap:16px;font-size:11.5px;color:var(--muted);padding:0 2px}
dialog{border:1px solid #cad7e8;border-radius:16px;padding:0;max-width:820px;width:calc(100vw - 40px);max-height:82vh;box-shadow:0 28px 80px -24px rgba(11,18,32,.4)}
dialog::backdrop{background:rgba(11,18,32,.5);backdrop-filter:blur(3px)}
.dialog-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 18px;border-bottom:1px solid #243951;position:sticky;top:0;background:linear-gradient(120deg,#0c192b,#13243a);z-index:1}
.dialog-head h2{font-size:16px;color:#fff;font-weight:700}
.dialog-head button{color:#dbe8ff;background:#ffffff0d;border-color:#ffffff30}
.dialog-head button:hover{background:#ffffff1f;border-color:#ffffff65}
.dialog-body{padding:16px 18px 22px;overflow:auto;max-height:calc(82vh - 60px)}
.dialog-body h3{font-size:14px;margin:19px 0 7px;color:var(--ink);font-weight:750}
.dialog-body h3:first-child{margin-top:0}
.dialog-body p{font-size:13px;line-height:1.7;color:#152238;margin:4px 0}
.dialog-body table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
.dialog-body th,.dialog-body td{text-align:left;padding:7px 9px;border-bottom:1px solid #e6ebf2;vertical-align:top;word-break:break-word}
.dialog-body th{width:110px;color:#4a5a72;background:#f6f8fc;font-weight:650;white-space:nowrap}
.dialog-body a{color:var(--brand);text-underline-offset:3px}
.dialog-body ol,.dialog-body ul{margin:6px 0;padding-left:20px;font-size:12px;line-height:1.7}
.dialog-body li{padding:2px 0;word-break:break-word}
.dialog-body small{display:block;color:var(--muted);font-size:10.5px}
.formula{background:#f5f8fd;border:1px solid #dce5f2;border-radius:14px;padding:10px 12px;font-size:12px;line-height:1.8}
.formula code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;color:#244772}

@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation-duration:.001ms!important;animation-delay:0ms!important;transition-duration:.001ms!important}}
@media (max-width:1120px){
  body{overflow:auto}
  .shell{height:auto;min-height:100vh}
  .workspace{grid-template-columns:1fr}
  .metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
  .title{white-space:normal}
  .side{position:static;max-height:none;overflow:visible}
  .panel{max-height:none}
  .cards,.bars,.rings{max-height:none;overflow:visible}
}
@media (max-width:720px){
  .shell{padding:16px}
  .metrics{grid-template-columns:1fr}
  .toolbar{grid-template-columns:1fr 1fr}
  .mini-stats{grid-template-columns:1fr 1fr}
  footer{flex-direction:column;gap:3px}
}

/* ---------- 问题证据与条件复现概率：保留圆环版式，区别购买后发生率 ---------- */
.risk-alert{--evidence-soft:var(--none-soft);--evidence-border:var(--line);--evidence-ink:var(--none-ink);display:flex;gap:13px;align-items:flex-start;padding:14px 17px;border:1px solid var(--evidence-border);border-radius:14px;background:var(--evidence-soft);box-shadow:var(--shadow);transition:background-color .35s,border-color .35s}
.risk-alert .badge{font-size:13px;font-weight:720;padding:5px 11px;border-radius:999px;white-space:nowrap;background:#ffffffcc;color:var(--evidence-ink);border:1px solid var(--evidence-border)}
.risk-alert .body{display:flex;flex-direction:column;gap:5px;min-width:0;flex:1}
.risk-alert .warning-title{font-size:16px;font-weight:720;color:var(--evidence-ink);line-height:1.55}
.risk-alert .line{font-size:12px;color:#4a5a72;line-height:1.65}
.risk-alert .warning-link{align-self:flex-start;color:var(--evidence-ink);font-size:12px;background:#ffffffcc;border-color:var(--evidence-border);padding:5px 11px}
.sample-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.sample-control{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.sample-control select{border:1px solid var(--line);border-radius:8px;padding:6px 9px;background:#fff;font-size:12px}
.sample-rings{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:13px 16px 15px}
.sample-card{display:flex;gap:16px;align-items:center;border:1px solid var(--line);border-radius:12px;padding:13px 16px;background:#f7f9fc;color:#4a5a72}
.sample-card .sample-copy{min-width:0;flex:1}.sample-card h3{font-size:15px;line-height:1.45;margin:0 0 4px}
.sample-card .sample-count{font-size:14px;font-weight:650;font-variant-numeric:tabular-nums}.sample-card .sample-note{font-size:11.5px;color:#63718a;margin:3px 0 0}
.sample-caveat{padding:9px 16px 12px;border-top:1px dashed var(--line);font-size:11.5px;color:var(--muted)}
.sample-disclosure>summary,.secondary-label{padding:12px 17px;font-size:13px;font-weight:650;color:#4a5a72;cursor:pointer}
.sample-disclosure>summary{list-style:revert}
.sample-disclosure[open]>summary{border-bottom:1px solid var(--line)}
.evidence-rings{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:14px 16px}
.evidence-card,.problem-card,.issue-row{--evidence-tone:var(--none);--evidence-soft:var(--none-soft);--evidence-ink:var(--none-ink);--evidence-border:var(--line);background:var(--evidence-soft);color:var(--evidence-ink);transition:background-color .35s,border-color .35s,color .35s}
.evidence-card{display:flex;gap:16px;align-items:center;border:1px solid var(--evidence-border);border-radius:14px;padding:14px 16px;box-shadow:inset 0 2px 0 var(--evidence-tone),0 3px 12px rgba(15,23,42,.025)}
.evidence-copy{min-width:0;flex:1}.evidence-copy h3{font-size:15px;line-height:1.45;margin-bottom:5px}
.evidence-name{font-size:14px;font-weight:750;color:var(--ink)}
.evidence-label{font-size:12px;font-weight:650;color:var(--evidence-ink)}
.probability-label{font-size:13px;font-weight:650;color:var(--ink);margin-top:5px}
.probability-details p{font-size:11.5px;line-height:1.6;margin:4px 0 0;color:#4a5a72}
.probability-details .probability-interval{font-size:12px;font-weight:600;color:var(--ink)}
.probability-details .probability-unstable{color:#4a5a72;font-weight:650}
.attention-label{margin:4px 0}.evidence-card.is-safety,.problem-card.is-safety{border-color:#eaa69c}
.evidence-meta,.evidence-reason{font-size:12px;line-height:1.65;color:#4a5a72;margin:4px 0 0}
.evidence-gauge{width:104px;display:flex;flex-direction:column;align-items:center;gap:6px;flex:none}
.evidence-card>.evidence-gauge{align-self:flex-start;margin-top:30px}
.evidence-progress-note{font-size:10.5px;line-height:1.5;text-align:center;color:#4a5a72}
.evidence-visual{position:relative;width:104px;aspect-ratio:1/1;flex:none;color:var(--evidence-tone)}
.evidence-visual svg{display:block;width:100%;height:100%;transform:rotate(-90deg)}
.evidence-track{fill:none;stroke:#ffffffd9;stroke-width:10}.evidence-fill{fill:none;stroke:var(--evidence-tone);stroke-width:10;stroke-linecap:round;transition:stroke-dashoffset .7s cubic-bezier(.22,.9,.28,1),stroke .35s}
.evidence-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.35}
.evidence-center strong{font-size:23px;font-weight:750;font-variant-numeric:tabular-nums}.evidence-center small{font-size:10px;color:#63718a;max-width:95%;text-align:center}
.evidence-note{padding:0 16px 13px;font-size:12px;color:var(--muted)}
.evidence-details{margin:0 16px 16px;border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden}
.evidence-details>summary{padding:11px 13px;font-size:13px;font-weight:650;color:#065cdb;cursor:pointer}
.evidence-problems{padding:0 12px 12px}.problem-card{border:1px solid var(--evidence-border);border-radius:14px;overflow:hidden}
.problem-card>summary{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px;cursor:pointer;list-style:none}
.problem-card>summary::-webkit-details-marker{display:none}
.problem-card .evidence-gauge,.problem-card .evidence-visual{width:82px}.problem-card .evidence-center strong{font-size:21px}
.problem-analysis{border-top:1px solid #ffffffb0;padding:8px 12px 12px;font-size:12px;line-height:1.7}
.problem-analysis p{margin:3px 0 7px}.problem-analysis button{margin-top:7px}
.problem-analysis ul{margin:5px 0;padding-left:18px}
.issue-row{display:grid;grid-template-columns:minmax(0,1fr) 104px;gap:12px;align-items:center;border-color:var(--evidence-border)}
.issue-row:hover{background:var(--evidence-soft);border-color:var(--evidence-tone)}
.issue-content{display:flex;flex-direction:column;gap:5px;min-width:0}.issue-signal{display:flex;flex-direction:column;align-items:center;gap:3px}
.issue-signal .evidence-gauge,.issue-signal .evidence-visual{width:82px}.issue-signal .evidence-center strong{font-size:18px}.issue-signal .evidence-center small{font-size:9px}
.issue-signal .evidence-label{font-size:10px;text-align:center}
.issue-rate{font-size:11.5px;font-variant-numeric:tabular-nums;color:#4a5a72}.issue-rate strong{color:var(--evidence-ink)}
@media(max-width:900px){.sample-rings,.evidence-rings{grid-template-columns:1fr}.sample-card,.evidence-card{padding:12px}.evidence-card .evidence-gauge,.evidence-card .evidence-visual{width:94px}.risk-alert{flex-wrap:wrap}}

/* v3.15: 清晰的阅读顺序与自适应证据工作区 */
html,body{width:100%;max-width:100%;overflow-x:clip}
.shell{width:100%;min-width:0;max-width:1500px;padding:22px clamp(14px,2vw,26px);gap:18px}
.shell>*{min-width:0;max-width:100%}
.top{gap:14px;flex-wrap:wrap}.top>div{min-width:0;max-width:100%}.header-actions{flex-wrap:wrap}
.title{white-space:normal;font-size:27px;overflow-wrap:anywhere;letter-spacing:-.5px}
.section-kicker{font-size:10px;letter-spacing:.12em;color:var(--brand);font-weight:750;margin-bottom:5px}
.section-heading{margin:14px 0 14px;padding-left:13px;border-left:3px solid var(--brand)}.section-heading h2{font-size:22px;color:var(--ink);font-weight:780;letter-spacing:-.4px}.section-heading p{font-size:12px;color:var(--muted);margin:4px 0 0}
.workspace{grid-template-columns:minmax(240px,.72fr) minmax(0,1.7fr);gap:16px;min-width:0}
.workspace>*,.side,.panel,.cards,.issue-list,.problem-card,.aspect-card{min-width:0;max-width:100%}
.side{position:static;max-height:none;overflow:visible;padding:0}
.overview-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:12px;padding:16px}
.overview-grid>*{min-width:0}.evidence-rings{padding:0;gap:12px;grid-template-columns:repeat(2,minmax(0,1fr))}
.evidence-card{height:100%;display:flex;flex-direction:column;align-items:stretch;gap:8px;padding:17px;background:var(--evidence-soft);min-width:0}
.evidence-card .evidence-gauge{align-self:center;margin:0;width:96px}.evidence-card .evidence-visual{width:96px}
.evidence-card h3{font-size:13px;color:#152238;margin:0 0 10px;font-weight:750}.evidence-card .evidence-name{font-size:15px;overflow-wrap:anywhere}
.evidence-card .evidence-copy{display:flex;flex-direction:column;gap:4px}.evidence-card .evidence-meta{font-size:11px;margin:0}
.evidence-card .summary-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;min-width:0}.summary-top>.evidence-copy{min-width:0}
.decision-card{--evidence-tone:var(--none);--evidence-soft:var(--none-soft);--evidence-border:var(--line);border:1px solid var(--evidence-border);border-radius:14px;background:var(--evidence-soft);padding:17px;height:100%;box-shadow:inset 0 2px 0 var(--evidence-tone),0 3px 12px rgba(15,23,42,.025)}
.decision-card h3{font-size:13px;color:#152238;margin:0 0 10px;font-weight:750}.decision-card .evidence-gauge{margin:0 auto 8px}.decision-card p{font-size:12px;line-height:1.55;margin:5px 0;color:#4a5a72}
.decision-card .decision-label{font-weight:750;color:var(--ink);font-size:15px}
.score-gauge{--evidence-tone:var(--none);--evidence-soft:#f1f4f7;width:120px;margin:12px auto}.score-gauge .evidence-visual{width:120px}.score-gauge .evidence-center strong{font-size:29px}.score-gauge .evidence-center small{font-size:11px}
.score-description{font-size:12px;text-align:center;margin:0 16px 16px;color:#63718a;overflow-wrap:anywhere}
.risk-distribution{display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:0 16px 13px;font-size:12px;color:#63718a}.dimension-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid #d3e2f8;border-radius:7px;padding:4px 10px;background:#f0f5ff;color:#21436f;font-weight:600}
.overview-scope{font-size:11px;color:#63718a;padding:0 16px 13px}
.estimate-brief{font-size:11px;line-height:1.6;color:#63718a;margin:4px 0}.estimate-button{font-size:11px;padding:5px 9px;background:#fff;align-self:flex-start;white-space:normal}
.attention-label{white-space:normal;overflow-wrap:anywhere;max-width:100%;font-size:10.5px}
.risk-alert{padding:12px 16px;align-items:center;gap:12px}.risk-alert .warning-title{font-size:14px}.risk-alert .badge{white-space:normal;font-size:11px}.risk-alert .warning-link{margin-left:auto;flex:none}.risk-alert .line{font-size:11px}
.rings{padding:6px 10px}.ring-aspects[hidden]{display:none}.bar-foot{flex-wrap:wrap;font-size:11px}.bar-name{white-space:normal;overflow-wrap:anywhere;font-size:13px}
.toolbar{grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);padding:10px 12px}.toolbar>*{width:100%;min-width:0}
.cards{padding:10px 12px 14px}.aspect-card>summary{flex-wrap:wrap;gap:8px}.head-text{min-width:120px}.head-text .name,.head-text small{white-space:normal;overflow-wrap:anywhere}.head-support{min-width:0}.head-support small{display:none}.aspect-card>summary>.tag{display:none}.aspect-card>summary>.tag.high{display:inline-flex}.mini-stats{grid-template-columns:repeat(2,minmax(0,1fr));margin:8px 0}.aspect-body{min-width:0}.mini{min-width:0}.mini b{font-size:13px;overflow-wrap:anywhere}
.claim>summary{flex-wrap:wrap}.claim-title{white-space:normal;overflow-wrap:anywhere;min-width:130px}.claim-count{font-size:11px}.promo,.finding,.ev-text,.ev-meta{overflow-wrap:anywhere;word-break:break-word}.ev-meta{font-size:10px}
.issue-panel{margin-top:0}.issue-list{grid-template-columns:minmax(0,1fr);padding:12px;gap:10px}.issue-row{grid-template-columns:minmax(0,1fr) 80px;gap:12px;padding:14px;cursor:default}.issue-name{overflow-wrap:anywhere;font-size:14px}.issue-top{align-items:flex-start;min-width:0;flex-wrap:wrap}.issue-content{gap:5px}.issue-signal{min-width:0;max-width:80px}.issue-signal .evidence-gauge,.issue-signal .evidence-visual{width:76px}.issue-signal .attention-label{display:none}.issue-quote{-webkit-line-clamp:2}.issue-details{grid-column:1/-1;min-width:0;border-top:1px solid var(--evidence-border);padding-top:8px;margin-top:4px}.issue-details>summary{font-size:12px;cursor:pointer;color:var(--brand);font-weight:650}.issue-details .evidence{background:#ffffffdd}.issue-details button{margin-top:8px;font-size:12px}
.issue-filter{width:100%;text-align:left}.issue-filter.active{background:#edf4ff}.extra-bar-rate{font-weight:650;font-size:13px;color:var(--evidence-tone)}
.panel-head input{min-width:0;max-width:180px;width:100%;padding:6px 9px;font-size:12px;border:1px solid var(--line);border-radius:9px}
.evidence-archive>summary{padding:13px 18px;cursor:pointer;font-size:12px;color:#152238;font-weight:650}.problem-card>summary{min-width:0}.problem-card .evidence-copy{min-width:0}.problem-card .evidence-meta{font-size:11px}.evidence-problems{grid-template-columns:minmax(0,1fr)}
.dialog-body,.dialog-head{min-width:0;overflow-wrap:anywhere}.dialog-head h2{min-width:0}.dialog-body .probability-details{margin:0 0 12px}.evidence-progress-note{display:none}
@media(max-width:1100px){.workspace{grid-template-columns:minmax(215px,.72fr) minmax(0,1.7fr)}.metrics{grid-template-columns:repeat(4,minmax(0,1fr))}.metric{padding:12px;gap:9px}.metric .icon{width:32px;height:32px}.metric b{font-size:26px}.metric small{white-space:normal;font-size:10px}.evidence-rings{grid-template-columns:repeat(2,minmax(0,1fr))}.evidence-card{padding:12px}.overview-grid{padding:12px;gap:10px}.evidence-card .summary-top{flex-direction:column}.evidence-card .evidence-gauge{align-self:center}}
@media(max-width:800px){.workspace{grid-template-columns:minmax(0,1fr)}.side{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.overview-grid{grid-template-columns:minmax(0,1fr)}.decision-card{display:flex;flex-direction:column}.evidence-rings{grid-template-columns:repeat(2,minmax(0,1fr))}.metric .icon{display:none}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.risk-alert{flex-wrap:wrap}.risk-alert .warning-link{margin-left:0}}
@media(max-width:480px){.shell{padding:12px;gap:14px}.top{padding:14px}.title{font-size:19px}.header-actions button{padding:6px 9px;font-size:11px}.evidence-rings,.side{grid-template-columns:minmax(0,1fr)}.evidence-card .summary-top{flex-direction:row}.head-text{min-width:100px}.issue-row{grid-template-columns:minmax(0,1fr) 70px;gap:8px}.issue-signal{max-width:70px}.issue-signal .evidence-gauge,.issue-signal .evidence-visual{width:68px}.toolbar{grid-template-columns:minmax(0,1fr)}.panel-head{padding:12px}.head-split{flex-wrap:wrap}.evidence-center strong{font-size:20px}}

</style>
</head>
<body>
<main class="shell">
  <header class="top">
    <div style="min-width:0">
      <div class="eyebrow">Product Experience Review</div>
      <h1 class="title" id="title"></h1>
      <div class="subtitle" id="subtitle"></div>
    </div>
    <div class="header-actions">
      <button id="productButton" type="button"><svg viewBox="0 0 24 24"><path d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h4"/></svg>商品宣传信息</button>
      <button id="methodButton" type="button"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v5h1"/></svg>分析口径</button>
    </div>
  </header>

  <section id="metrics" class="metrics" aria-label="采集概况"></section>
  <div id="notice" class="notice" hidden></div>
  <section id="riskAlert" class="risk-alert" hidden aria-label="额外体验问题提醒"></section>
  <section class="panel overview-panel" id="issueEvidencePanel" aria-label="风险总览">
    <div class="panel-head sample-head">
      <div><div class="section-kicker">01 / 总体判断</div><h2>风险总览</h2></div>
      <label class="sample-control" for="evidenceWindow">近期范围
        <select id="evidenceWindow" aria-label="选择问题证据的近期观察窗口"></select>
      </label>
    </div>
    <div class="overview-grid"><div id="overallDecision"></div><div id="evidenceRings" class="evidence-rings"></div></div>
    <div id="riskDistribution" class="risk-distribution"></div>
    <div id="evidenceCaveat" class="overview-scope"></div>
  </section>

  <section class="analysis-section" id="advertisedSection" aria-label="宣传落差分析">
    <div class="section-heading"><div class="section-kicker">02 / 宣传与体验对照</div><h2>宣传落差分析</h2><p>先看矛盾集中在哪些方面，再核对宣传原文与评论。</p></div>
    <div class="workspace">
      <aside class="side">
        <section class="panel"><div class="panel-head"><h2>宣传矛盾指数</h2></div><div id="rings" class="rings one"></div><div id="ringAspects" class="ring-aspects" hidden></div></section>
        <section class="panel grow">
          <div class="panel-head head-split"><div><h2>主要矛盾维度</h2><div class="desc" id="barCount"></div></div><button id="toggleZero" class="inline-button" type="button" hidden></button></div>
          <div id="bars" class="bars"></div>
        </section>
      </aside>
      <section class="panel grow">
        <div class="panel-head head-split"><div><h2>宣传对照与评论依据</h2><div class="desc" id="resultCount"></div></div><button id="collapseButton" class="inline-button" type="button">收起全部</button></div>
        <div class="toolbar"><input id="search" type="search" placeholder="搜索维度、宣传或评论" aria-label="搜索报告"><select id="aspect" aria-label="选择方面"></select><select id="risk" aria-label="筛选宣传矛盾指数档位"><option value="">全部档位</option><option value="high">高矛盾</option><option value="medium">中等矛盾</option><option value="low">低矛盾</option><option value="none">待评估</option></select><select id="sort" aria-label="排序"><option value="risk">按矛盾指数</option><option value="evidence">按明确证据</option><option value="partial">按关联证据</option></select></div>
        <div id="cards" class="cards"></div>
      </section>
    </div>
  </section>

  <section class="analysis-section" id="extraSection" aria-label="额外体验问题分析">
    <div class="section-heading"><div class="section-kicker">03 / 其他需要留意的体验</div><h2>额外体验问题</h2><p>未找到对应宣传，或尚未建立直接矛盾关系的负面反馈。</p></div>
    <div class="workspace">
      <aside class="side"><section class="panel"><div class="panel-head"><h2>额外风险指数</h2></div><div id="extraIndex" class="rings one"></div></section><section class="panel"><div class="panel-head"><h2>问题分布与复现概率</h2></div><div id="extraBars" class="bars"></div></section></aside>
      <section class="panel grow issue-panel" id="issuePanel"><div class="panel-head head-split"><div><h2>具体问题与评论依据</h2><div class="desc" id="issueCount"></div></div><input id="extraSearch" type="search" placeholder="搜索问题" aria-label="搜索宣传外问题"></div><div id="issues" class="issue-list"></div></section>
    </div>
  </section>

  <details class="panel evidence-archive"><summary id="evidenceProblemCount">全部具体问题</summary><div id="evidenceProblems" class="issue-list evidence-problems"></div></details>
  <details class="panel sample-disclosure" id="sampleScope">
    <summary>采集范围与样本明细</summary><div class="panel-head sample-head"><div><h2>样本明细</h2></div><label class="sample-control" for="sampleDenominator">统计范围<select id="sampleDenominator" aria-label="统一切换负面样本占比分母"><option value="scanned">扫描评论</option><option value="eligible">可分析低星正文</option></select></label></div><div id="sampleRings" class="sample-rings"></div><div id="sampleCaveat" class="sample-caveat"></div>
  </details>
  <footer><span>商品宣传与体验落差分析</span><span>__VIEW_MODE__ · 选择文件后自主导出</span></footer>
</main>

<dialog id="detailDialog">
  <div class="dialog-head">
    <h2 id="dialogTitle"></h2>
    <button id="closeDialog" type="button" aria-label="关闭详情">关闭</button>
  </div>
  <div class="dialog-body" id="dialogBody"></div>
</dialog>

<script id="reportData" type="application/json">__REPORT_DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("reportData").textContent);
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = String(text);
  return node;
};
const SVGNS = "http://www.w3.org/2000/svg";
const TONE = { confirmed: "high", possible: "medium", none: "low", insufficient: "insufficient", not_evaluated: "none" };
const BAND_TONE = { high: "high", medium: "medium", low: "low", none: "none" };
const DECISION = D.report_decision || {};
function decisionWindow() { return (DECISION.windows || {})[evidenceWindowDays] || {}; }
function advertisedAspects() {
  const scores = new Map(((decisionWindow().advertised || {}).aspects || DECISION.advertised_aspects || []).map(a => [a.aspect, a]));
  return (D.aspects || []).filter(a => a.claim_count > 0).map(a => {
    const revised = scores.get(a.aspect);
    return revised ? {...a, contradiction_count:revised.contradiction_count, risk_index:revised.index, risk_band:revised.band || "none",
      risk_band_label:revised.band_label || ({high:"高",medium:"中",low:"低"}[revised.band]) || "待评估", decision:revised} : a;
  });
}
function toneOf(aspect) { return BAND_TONE[aspect.risk_band] || TONE[aspect.evidence_status] || "none"; }
const STATUS_ORDER = { confirmed: 4, possible: 3, none: 2, insufficient: 1, not_evaluated: 0 };
const gaps = new Map((D.gaps || []).map((g) => [g.claim_id, g]));
const O = D.overview || {};
const S = D.sampling || {};
const reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
let expanded = true;
let showZero = false;
let sampleDenominator = "scanned";
const IE = D.issue_evidence || {};
let evidenceWindowDays = String([7, 30, 90].includes(Number(IE.default_window_days)) ? Number(IE.default_window_days) : 30);

const fmtRate = (value, digits) => (value == null ? "未评估" : (value * 100).toFixed(digits == null ? 2 : digits) + "%");
const fmtCI = (ci) => (ci ? (ci[0] * 100).toFixed(2) + "%–" + (ci[1] * 100).toFixed(2) + "%" : "—");
const fmtCount = (value) => (value == null ? "0" : String(value));

/* ---------- 近期问题证据：只展示后端评估，不从占比或数量推算关注级别 ---------- */
const countOrNull = (value) => value == null || value === "" || !Number.isInteger(Number(value)) || Number(value) < 0 ? null : Number(value);
function currentEvidenceWindow() {
  return (IE.windows || {})[evidenceWindowDays] || null;
}
function evidenceState(assessment) {
  const known = !!assessment && typeof assessment === "object" && typeof assessment.attention_level === "string";
  const source = known ? assessment : {};
  const tone = known && ["high", "medium", "low", "none"].includes(source.tone) ? source.tone : "none";
  const count = known ? countOrNull(source.repeat_count) : null;
  // 证据不完整时，零条已确认记录不能呈现成“近期没有问题”；原始计数保留在展开依据。
  return { known, source, tone, count: source.attention_level === "incomplete" && count === 0 ? null : count,
    label: known ? source.attention_label || "评估说明未提供" : "未评估" };
}
function probabilityState(assessment) {
  const source = assessment && typeof assessment.probability_estimate === "object" && assessment.probability_estimate || {};
  const rate = (value) => value == null || value === "" || !Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 1 ? null : Number(value);
  const estimate = rate(source.estimate), successes = countOrNull(source.successes), total = countOrNull(source.total);
  const rawInterval = Array.isArray(source.interval) && source.interval.length === 2 ? source.interval.map(rate) : [];
  const interval = rawInterval.length === 2 && rawInterval.every((value) => value != null) && rawInterval[0] <= rawInterval[1] ? rawInterval : null;
  const level = rate(source.credible_level);
  const valid = source.status === "estimated" && source.target === "next_comparable_low_star_review_mentions_problem" &&
    estimate != null && interval != null && level != null && level > 0 && level < 1 &&
    successes != null && total != null && total > 0 && successes <= total;
  // Color follows the estimate. Sample size remains an uncertainty note, not a hue override.
  const tone = valid ? estimate >= .6 ? "high" : estimate >= .3 ? "medium" : "low" : "none";
  return {source, valid, estimate: valid ? estimate : null, interval: valid ? interval : null,
    credibleLevel: level, successes, total, tone, smallSample: !!source.small_sample};
}
function probabilityPalette(probability) {
  if (!probability.valid) return {color:"#8a97a6", soft:"#f1f4f7", border:"#dce4ed", ink:"#4d6073", glow:"#edf2f9", page:"#f6f8fc"};
  const p = Math.max(0, Math.min(1, probability.estimate));
  const anchors = [[21,151,101], [214,154,43], [217,75,84]];
  const half = p <= .5 ? 0 : 1, t = p <= .5 ? p * 2 : (p - .5) * 2;
  const color = anchors[half].map((c, i) => c + (anchors[half + 1][i] - c) * t);
  const hex = values => "#" + values.map(c => Math.round(c).toString(16).padStart(2,"0")).join("");
  const white = weight => hex(color.map(c => c * (1 - weight) + 255 * weight));
  return {color:hex(color), soft:white(.945), border:white(.77),
    ink:hex(color.map((c,i) => c * .25 + [29,43,66][i] * .75)), glow:white(.88), page:white(.981)};
}
function applyProbabilityPalette(node, palette) {
  node.style.setProperty("--evidence-tone", palette.color);
  node.style.setProperty("--evidence-soft", palette.soft);
  node.style.setProperty("--evidence-ink", palette.ink);
  node.style.setProperty("--evidence-border", palette.border);
  node.dataset.probabilityColor = palette.color;
}
function probabilityIntervalLine(state) {
  const label = state.credibleLevel == null ? "模型参数可信区间" : (state.credibleLevel * 100).toFixed(0) + "%模型可信区间";
  return label + "：" + (state.interval ? state.interval.map((value) => fmtRate(value, 1)).join("–") : "暂不能估计");
}
function attentionBadge(assessment) {
  const state = evidenceState(assessment);
  const badge = el("span", "tag " + state.tone + " attention-label", state.label);
  const palette = probabilityPalette(assessment && assessment.attention_level === "safety" ? {valid:true,estimate:1} : probabilityState(assessment));
  badge.style.color = palette.ink; badge.style.backgroundColor = palette.soft; badge.style.borderColor = palette.border;
  return badge;
}
function appendProbabilitySummary(box, assessment, compact) {
  const state = probabilityState(assessment), source = state.source, details = el("div", "probability-details");
  if (!compact) details.append(el("div", "probability-label", "同类问题复现概率"));
  details.append(el("p", "probability-interval", probabilityIntervalLine(state)),
    el("p", "probability-counts", "k/n = " + (state.successes == null ? "—" : state.successes) + "/" + (state.total == null ? "—" : state.total) + " · 同SKU支持 " + (state.successes == null ? "—" : state.successes) + " 条"),
    el("p", "probability-sku", "SKU范围：" + (source.sku_label || "尚未确认")));
  details.append(el("p", "probability-scope", "在可分析低星反馈中；不是购买后故障率"));
  if (state.smallSample) details.append(el("p", "probability-unstable", "样本较少，估计范围较宽。"));
  if (!state.valid) details.append(el("p", "probability-unavailable", source.reason || "缺少可比反馈，暂不能估计。"));
  box.append(details);
}
function estimateBrief(box, assessment) {
  const state = probabilityState(assessment);
  box.append(el("p", "estimate-brief", state.valid ? state.successes + " 条支持 / " + state.total + " 条可比反馈" + (state.smallSample ? " · 样本较少" : "") : "可比样本不足"));
  const button = el("button", "estimate-button", "查看估计依据"); button.type = "button";
  button.addEventListener("click", event => { if (event.stopPropagation) event.stopPropagation(); const body = el("div"); appendEvidenceAnalysis(body, assessment); openDialog("概率估计依据", body); });
  box.append(button);
}
function evidenceThreshold() {
  return countOrNull((IE.policy || {}).repeat_threshold) || 4;
}
function evidenceCountLine(assessment) {
  const state = evidenceState(assessment);
  return state.count == null ? "近期同问题有效反馈：未评估" : "近期同问题有效反馈 " + state.count + " 条";
}
function applyEvidenceTone(node, assessment) {
  const state = evidenceState(assessment), probability = probabilityState(assessment);
  applyProbabilityPalette(node, probabilityPalette(probability));
  node.dataset.evidenceTone = probability.tone;
  node.dataset.attentionTone = state.tone;
  node.dataset.probabilityStatus = probability.valid ? "estimated" : "insufficient";
  node.dataset.evaluated = String(state.known);
  node.dataset.evidenceWindow = evidenceWindowDays;
}
function evidenceVisual(assessment, mini) {
  const state = probabilityState(assessment), visual = el("div", "evidence-visual" + (mini ? " mini-ring" : ""));
  visual.setAttribute("role", "img");
  visual.setAttribute("aria-label", "同类问题复现概率：" + (state.valid ? fmtRate(state.estimate, 1) : "暂不能估计") + "；在可分析低星反馈中，不是购买后故障率；" + probabilityIntervalLine(state));
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 120 120"); svg.setAttribute("aria-hidden", "true");
  const radius = 50, circumference = 2 * Math.PI * radius;
  const track = document.createElementNS(SVGNS, "circle"), fill = document.createElementNS(SVGNS, "circle");
  [track, fill].forEach((node) => { node.setAttribute("cx", "60"); node.setAttribute("cy", "60"); node.setAttribute("r", radius); });
  track.setAttribute("class", "evidence-track"); fill.setAttribute("class", "evidence-fill");
  fill.setAttribute("stroke-dasharray", circumference); fill.style.strokeDashoffset = String(circumference);
  svg.append(track, fill);
  const center = el("div", "evidence-center");
  center.append(el("strong", "", state.valid ? fmtRate(state.estimate, 1) : "—"),
    el("small", "", state.valid ? "条件复现概率" : "暂不能估计"));
  visual.append(svg, center);
  if (state.valid) {
    const target = String(circumference * (1 - state.estimate));
    if (reduced) fill.style.strokeDashoffset = target;
    else requestAnimationFrame(() => { fill.style.strokeDashoffset = target; });
  }
  const gauge = el("div", "evidence-gauge");
  gauge.append(visual);
  return gauge;
}
function evidenceIdentity(source) {
  return source.identity_label || "用户身份未确认；去重评论不代表不同买家";
}
function evidenceWindowLabel(source) {
  return source.window_label || "近" + evidenceWindowDays + "天（具体日期范围未提供）";
}
function evidenceFacts(assessment) {
  const source = evidenceState(assessment).source, bits = [];
  const recent = countOrNull(source.recent_review_count), sameSku = countOrNull(source.max_same_sku_count);
  const total = countOrNull(source.review_count), specific = countOrNull(source.specific_count);
  if (recent != null) bits.push("近期去重评论 " + recent + " 条");
  if (sameSku != null) bits.push("同 SKU 最多 " + sameSku + " 条");
  if (specific != null) bits.push("含具体事实 " + specific + " 条");
  if (total != null) bits.push("所获历史共 " + total + " 条");
  return bits.join(" · ") || "次数、SKU 或具体性资料尚未齐备";
}
function appendEvidenceAnalysis(box, assessment) {
  const state = evidenceState(assessment), source = state.source;
  appendProbabilitySummary(box, assessment);
  box.append(el("h3", "", "重复反馈与安全提醒"), attentionBadge(assessment));
  box.append(el("p", "", "实际影响：" + (source.impact_label || "尚未分级，需核查具体使用情境")));
  box.append(el("p", "", evidenceWindowLabel(source) + " · " + evidenceCountLine(assessment)));
  box.append(el("p", "", evidenceFacts(assessment)));
  if (source.scope_label) box.append(el("p", "", "SKU 与适用范围：" + source.scope_label));
  box.append(el("p", "", source.reason || "尚无可用的问题证据评估；不能从评论占比推算关注级别。"));
  box.append(el("h3", "", "独立性与数据局限"));
  box.append(el("p", "", evidenceIdentity(source)));
  const identified = countOrNull(source.identified_source_count);
  if (identified != null) box.append(el("p", "", "可区分来源标签 " + identified + " 个；来源标签不等同于已核实的真实买家身份。"));
  const limitations = [...new Set([...(Array.isArray(source.limitations) ? source.limitations : []), ...(Array.isArray(IE.limitations) ? IE.limitations : [])])];
  if (limitations.length) {
    const list = el("ul"); limitations.forEach((note) => list.append(el("li", "", note))); box.append(list);
  }
  const probability = probabilityState(assessment);
  const coverage = probability.source.coverage;
  if (coverage && typeof coverage === "object") {
    const disclosure = el("details"), body = el("div");
    disclosure.append(el("summary", "", "抽取覆盖与排除记录"));
    body.append(el("p", "", "以下为当前窗口各已知规格的筛选合计，不是主圈所选SKU的n；n已按来源和文案去重。"),
      el("p", "", "逐评论抽取状态：" + (coverage.extraction_provided ? "已提供" : "未提供") +
        "；窗口内候选 " + (countOrNull(coverage.candidate_review_count) || 0) +
        " 条，完成并通过核对 " + (countOrNull(coverage.completed_review_count) || 0) + " 条。"));
    const labels = {ineligible_review:"不可分析正文", wrong_product:"非本商品", not_confirmed_low_star:"未确认低星", unknown_sku:"规格未知",
      unknown_date:"日期或截止日未知", future_date:"晚于截止日", outside_window:"窗口外日期", missing_extraction:"缺少抽取状态",
      incomplete_extraction:"抽取未完成或有错误", snapshot_mismatch:"抽取快照与正文或问题不一致", append_not_in_extraction_snapshot:"追评未纳入抽取快照"};
    const excluded = Object.entries(coverage.excluded || {});
    if (excluded.length) {
      const list = el("ul");
      excluded.forEach(([key, value]) => list.append(el("li", "", (labels[key] || key) + "：" + value)));
      body.append(list, el("p", "", "日期排除按事件计数，同一评论可能有初评和追评；以上数值不能直接相加当作买家人数。"));
    }
    disclosure.append(body); box.append(disclosure);
  }
  if (Array.isArray(probability.source.assumptions) && probability.source.assumptions.length) {
    box.append(el("h3", "", "条件模型假设"));
    const assumptions = el("ul"); probability.source.assumptions.forEach((note) => assumptions.append(el("li", "", note))); box.append(assumptions);
  }
  if (Array.isArray(probability.source.per_sku) && probability.source.per_sku.length) {
    const variants = el("details"), body = el("div");
    variants.append(el("summary", "", "按SKU查看条件复现估计"));
    probability.source.per_sku.forEach((item) => appendProbabilitySummary(body, {probability_estimate: {...probability.source, ...item, per_sku: []}}));
    variants.append(body); box.append(variants);
  }
  box.append(el("p", "basis", "主圈是条件概率模型估计，仅针对后续同类可分析低星反馈，不是购买后发生率。模型参数可信区间不包含评论选择、采集遗漏或分类偏差；较低条件概率不能排除严重后果。"));
  box.append(el("p", "basis", "同SKU达到" + evidenceThreshold() + "条是补充的经验预警规则，未经购买结局验证；跨SKU条数和不同问题不能合并凑足单一SKU同问题门槛。"));
}
function evidenceSummaryCard(assessment, title, key) {
  const state = evidenceState(assessment), source = state.source, card = el("div", "evidence-card");
  if (source.attention_level === "safety") card.className += " is-safety";
  card.dataset.signal = key; applyEvidenceTone(card, assessment);
  card.append(el("h3", "", title));
  const top = el("div", "summary-top"), copy = el("div", "evidence-copy");
  copy.append(el("div", "evidence-name", source.name || source.problem_name || "暂未形成可比估计"));
  if (source.attention_level === "safety") copy.append(attentionBadge(assessment));
  top.append(copy, evidenceVisual(assessment, false)); card.append(top); estimateBrief(card, assessment);
  return card;
}
function decisionAssessment(side) {
  const data = decisionWindow()[side];
  if (data) return data.probability_assessment || null;
  const windowData = currentEvidenceWindow() || {};
  // Legacy reports cannot relabel all problems as advertising contradictions.
  return side === "unadvertised" ? windowData.unadvertised : null;
}
function scoreValue(value) {
  return value != null && value !== "" && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 100 ? Number(value) : null;
}
function scoreGauge(score, label) {
  const valid = scoreValue(score) != null, palette = probabilityPalette({valid, estimate:valid ? Number(score)/100 : null});
  const gauge = el("div", "evidence-gauge score-gauge"); applyProbabilityPalette(gauge, palette);
  const visual = el("div", "evidence-visual"), svg = document.createElementNS(SVGNS,"svg");
  svg.setAttribute("viewBox","0 0 120 120"); svg.setAttribute("aria-hidden","true");
  const circumference = 2*Math.PI*50;
  ["evidence-track","evidence-fill"].forEach(cls => {const circle = document.createElementNS(SVGNS,"circle"); circle.setAttribute("class",cls); circle.setAttribute("cx",60);circle.setAttribute("cy",60);circle.setAttribute("r",50); if(cls === "evidence-fill"){circle.setAttribute("stroke-dasharray",circumference);circle.style.strokeDashoffset=String(circumference*(1-(valid?Number(score)/100:0)));}svg.append(circle);});
  const center = el("div","evidence-center"); center.append(el("strong","",valid?Number(score).toFixed(1):"—"),el("small","","/ 100")); visual.append(svg,center); visual.setAttribute("role","img");visual.setAttribute("aria-label",label+"："+(valid?Number(score).toFixed(1)+"分":"待评估"));gauge.append(visual);return gauge;
}
function renderEvidenceSummary() {
  const data = decisionWindow(), original = currentEvidenceWindow() || {}, overall = data.overall || {};
  const ad = decisionAssessment("advertised"), extra = decisionAssessment("unadvertised");
  const candidates = [ad,extra].map(probabilityState).filter(p => p.valid).sort((a,b)=>b.estimate-a.estimate);
  const index = scoreValue(overall.index);
  const probability = index == null ? candidates[0] || probabilityState(null) : {valid:true,estimate:index/100,tone:index>=65?"high":index>=35?"medium":"low"};
  const palette = probabilityPalette(probability);
  document.body.style.setProperty("--evidence-page-glow",palette.glow);document.body.style.setProperty("--evidence-page-bg",palette.page);
  document.body.dataset.evidenceTone=probability.tone;document.body.dataset.evidenceWindow=evidenceWindowDays;document.body.dataset.probabilityColor=palette.color;
  const host = $("evidenceRings");host.replaceChildren();
  host.append(evidenceSummaryCard(ad,"宣传相关问题复现概率","advertised"),evidenceSummaryCard(extra,"额外问题复现概率","unadvertised"));
  const container = $("overallDecision"), card = el("div","decision-card");container.replaceChildren();
  const score = scoreValue(overall.index), color = probabilityPalette({valid:score!=null,estimate:score==null?null:score/100});applyProbabilityPalette(card,color);
  card.append(el("h3","","综合关注指数"),scoreGauge(score,"综合关注指数"),el("div","decision-label",score==null?"资料尚不足，待评估":score>=65?"建议重点核查":score>=35?"存在需要核查的问题":overall.status==="partial"?"已知结果较低 · 仍有待评估项":"当前证据强度较低"));
  card.append(el("p","",overall.dominant === "unadvertised" ? "主要来自额外体验问题" : overall.dominant === "advertised" ? "主要来自宣传与体验的矛盾" : "综合宣传对照与额外问题判断"));
  const button = el("button","estimate-button","查看判断依据");button.type="button";button.addEventListener("click",()=>{const body=el("div");body.append(el("p","","综合关注指数取两类已知指数中较高的一项。某一类暂无证据不会冲淡另一类已有问题；缺项保留待评估。"),el("p","","这是用于安排核查顺序的0–100指数，不是购买后问题发生概率。"));openDialog("综合判断依据",body);});card.append(button);container.append(card);
  const dimensions = $("riskDistribution");dimensions.replaceChildren();dimensions.append(el("span","","主要关注维度"));
  const names = [...new Set([...(data.advertised && data.advertised.aspects || []).filter(a=>scoreValue(a.index)>0).map(a=>a.aspect),...(data.unadvertised && data.unadvertised.aspects || []).map(a=>a.aspect)])].filter(Boolean);
  (names.length?names.slice(0,5):[original.overall && original.overall.aspect].filter(Boolean)).forEach(name=>dimensions.append(el("span","dimension-chip",name)));
  if(!names.length&&!original.overall)dimensions.append(el("span","","待补充数据"));
  $("evidenceCaveat").textContent="概率范围：同规格可分析低星反馈中的问题复现。两个摘要各选一个主要问题。";
  renderExtraSummary();
}
function renderExtraSummary() {
  const data = decisionWindow().unadvertised || {}, assessment = decisionAssessment("unadvertised"), probability = probabilityState(assessment);
  const score = scoreValue(data.index);
  $("extraIndex").replaceChildren(scoreGauge(score,"额外风险指数"));
  $("extraIndex").append(el("div","score-description",data.top_aspect || (assessment||{}).aspect || "宣传外体验问题"));
  const host = $("extraBars");host.replaceChildren();
  const rows = data.aspects || [];
  rows.forEach(row=>{const a=row.assessment || {name:row.name,probability_estimate:row.probability_estimate},p=probabilityState(a),button=el("button","bar-row issue-filter"+(extraAspect===row.aspect?" active":"")), palette=probabilityPalette(p);applyProbabilityPalette(button,palette);button.type="button";const top=el("div","bar-top");top.append(el("span","bar-name",row.aspect),el("span","extra-bar-rate",p.valid?fmtRate(p.estimate,1):"—"));const track=el("div","track"),fill=el("div","fill");fill.style.width=(p.valid?p.estimate*100:0)+"%";fill.style.background=palette.color;track.append(fill);button.append(top,track,el("div","bar-foot",(row.assessment||{}).name||row.problem_name||row.name||"查看该维度的问题"));button.addEventListener("click",()=>{extraAspect=extraAspect===row.aspect?"":row.aspect;renderExtraSummary();renderIssues();});host.append(button);});
  if(!rows.length)host.append(emptyBlock("暂无可比维度","可在右侧查看已发现的问题。"));
}
function renderEvidenceProblems() {
  const data = currentEvidenceWindow(), rows = data && Array.isArray(data.problems) ? data.problems : [];
  const host = $("evidenceProblems"); host.replaceChildren();
  $("evidenceProblemCount").textContent = rows.length ? "完整问题清单（" + rows.length + "）" : "完整问题清单";
  if (!rows.length) {
    host.append(emptyBlock(data ? "当前窗口暂无已归类的具体问题" : "问题证据尚未评估", "没有记录或资料不足不代表商品安全；可继续核查采集范围及下方评论原文。")); return;
  }
  rows.forEach((row) => {
    const card = el("details", "problem-card"); applyEvidenceTone(card, row); card.dataset.problem = row.problem_id || row.name || "";
    if (row.attention_level === "safety") card.className += " is-safety";
    const summary = el("summary"), copy = el("div", "evidence-copy");
    copy.append(el("div", "evidence-name", row.name || row.problem_name || "未命名问题"),
      el("div", "evidence-meta", (row.aspect || "未归类方面") + " · " + evidenceWindowLabel(row)),
      attentionBadge(row), el("div", "evidence-meta", "实际影响：" + (row.impact_label || "尚未分级")));
    copy.append(el("div", "evidence-meta", evidenceCountLine(row)));
    summary.append(copy, evidenceVisual(row, true));
    const analysis = el("div", "problem-analysis"); appendEvidenceAnalysis(analysis, row);
    const button = el("button", "", "查看全部评论原文"); button.type = "button";
    button.addEventListener("click", () => openIssueReviews(row, row)); analysis.append(button);
    card.append(summary, analysis); host.append(card);
  });
}

/* ---------- 样本比例：中性背景描述，不影响问题提醒、主配色和排序 ---------- */
function sampleStats(signal) {
  const source = signal || {};
  const value = source[sampleDenominator] || {};
  const numeric = (number) => number == null || number === "" || !Number.isFinite(Number(number)) ? null : Number(number);
  const numerator = numeric(value.numerator == null ? source.review_count : value.numerator);
  const denominator = numeric(value.denominator);
  const rate = denominator != null && denominator > 0 ? numeric(value.rate) : null;
  const valid = rate != null && rate >= 0;
  return { numerator, denominator, rate: valid ? rate : null,
    ci95: valid && Array.isArray(value.ci95) ? value.ci95 : null, small_sample: !!value.small_sample || (denominator > 0 && denominator < 30), basis: source.basis || S.source || "analysis_input" };
}
function sampleBasis(signal) {
  if (sampleDenominator === "eligible") return "可分析低星正文";
  return (signal || {}).basis === "collection_info" || (!(signal || {}).basis && S.source === "collection_info") ? "扫描评论" : "分析输入评论";
}
function sampleCountLine(stats) {
  return (stats.numerator == null ? "—" : stats.numerator) + " / " + (stats.denominator > 0 ? stats.denominator : "—") + " 条";
}
function sampleRateLine(signal) {
  const stats = sampleStats(signal);
  return sampleBasis(signal) + "：" + sampleCountLine(stats) + (stats.rate == null ? " · 缺少分母" : " = " + fmtRate(stats.rate, 1));
}
function sampleCard(signal, title, key) {
  const stats = sampleStats(signal), card = el("div", "sample-card");
  card.dataset.signal = key; card.dataset.denominator = sampleDenominator;
  const copy = el("div", "sample-copy");
  copy.append(el("h3", "", title), el("div", "sample-count", sampleRateLine(signal)));
  if (stats.ci95) copy.append(el("p", "sample-note", "95% 比例参考区间 " + fmtCI(stats.ci95)));
  if (stats.small_sample) copy.append(el("p", "sample-note", "分母不足30条，请结合原文谨慎判断。"));
  if (stats.numerator === 0) copy.append(el("p", "sample-note", "零记录不等于商品没有问题。"));
  card.append(copy); return card;
}
function renderSampleSummary() {
  const host = $("sampleRings"); host.replaceChildren();
  host.append(sampleCard(O.sample_risk, "全部已发现负面评论", "overall"),
    sampleCard(O.unadvertised_risk, "未对应商品宣传的负面评论", "unadvertised"));
  const selector = $("sampleDenominator"); selector.value = sampleDenominator;
  if (selector.options && selector.options.length) selector.options[0].textContent = sampleBasis({ basis: (O.sample_risk || {}).basis });
  $("sampleCaveat").textContent = "此处使用「" + sampleBasis(O.sample_risk) + "」为分母，只描述本次采集。比例与95%比例参考区间不校正选择偏差、漏采和模型误判，不是购买后出现问题的概率，也不参与上方问题预警、排序和配色。";
}

function countUp(node, target, format, duration) {
  if (reduced) { node.textContent = format(target); return; }
  const started = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    node.textContent = format(target * eased);
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function emptyBlock(title, note) {
  const box = el("div", "empty");
  box.append(el("b", "", title), el("span", "", note));
  return box;
}

/* ---------- 顶部与指标 ---------- */
$("title").textContent = D.title || "商品宣传与低星体验分析";
function formatTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ").replace(/([+-]\d{2}:?\d{2}|Z)$/, "").slice(0, 16);
}
const meta = ["商品 " + (D.product_id || "—"), formatTime(D.generated_at), (D.category || {}).label || "未识别品类"];
$("subtitle").textContent = meta.filter(Boolean).join(" · ");

const freshness = D.freshness || {};
const notices = [];
if (freshness.is_current === false) {
  notices.push("历史或不完整结果：" + (freshness.reasons || []).join("；"));
} else if (D.analysis_status && D.analysis_status !== "completed") {
  notices.push("分析尚未完整完成，当前只展示已经具备的数据。");
}
if (S.source === "collection_info" && S.scan_complete === false) {
  notices.push("采集未覆盖全部评论：" + (S.stop_reason || "达到采集上限") + "；比例只代表本次扫描样本。");
}
if (S.source !== "collection_info") {
  notices.push("未提供采集扫描总数，低星占比以本次分析输入为分母。");
}
if (notices.length && (freshness.is_current === false || D.analysis_status && D.analysis_status !== "completed")) {
  $("notice").hidden = false;
  $("notice").textContent = freshness.is_current === false ? "当前预览为历史或未完成结果" : "本次报告基于已扫描的评论";
  $("notice").title = notices.join(" ");
}

const ICONS = {
  doc: '<path d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h4"/>',
  alert: '<path d="M12 3.6 21 19a1 1 0 0 1-.9 1.5H3.9A1 1 0 0 1 3 19Z"/><path d="M12 9v5"/><path d="M12 17.2h.01"/>',
  users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3 19c0-3 2.7-5 6-5s6 2 6 5"/><path d="M16 6.2a3 3 0 0 1 0 5.6"/><path d="M18 18.6c0-2.2-1-4-2.8-4.8"/>',
  mega: '<path d="M4 10v4a1 1 0 0 0 1 1h3l5 4V5L8 9H5a1 1 0 0 0-1 1Z"/><path d="M17 9a4 4 0 0 1 0 6"/><path d="M19.5 6.5a8 8 0 0 1 0 11"/>'
};
const RK = O.risk_summary || {};
const metricRows = [
  {label:"扫描评论",value:fmtCount(O.scanned_review_count),note:"本次采集覆盖",tone:"var(--brand)",soft:"var(--brand-soft)",icon:"doc"},
  {label:"低星 / 中差评",value:fmtCount(O.low_star_review_count),note:"本次命中评论",tone:"var(--brand)",soft:"var(--brand-soft)",icon:"users"},
  {label:"有效评论正文",value:fmtCount(O.valid_review_count),note:"已纳入文本分析",tone:"var(--brand)",soft:"var(--brand-soft)",icon:"doc"},
  {label:"商品宣传维度",value:String((D.aspects||[]).filter(a=>a.claim_count>0).length),note:(D.claims||[]).length+" 条宣传主张",tone:"var(--brand)",soft:"var(--brand-soft)",icon:"mega"}
];
metricRows.forEach((row, index) => {
  const card = el("div", "metric");
  card.style.setProperty("--tone", row.tone);
  card.style.setProperty("--tone-soft", row.soft);
  card.style.animationDelay = (index * 55) + "ms";
  const icon = el("div", "icon");
  icon.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS[row.icon] + "</svg>";
  const meta2 = el("div", "meta");
  meta2.append(el("div", "label", row.label), el("b", "", row.value), el("small", "", row.note));
  card.append(icon, meta2);
  $("metrics").append(card);
});

/* ---------- 宣传矛盾方面明细 ---------- */
function renderRingAspects() { $("ringAspects").replaceChildren(); $("ringAspects").hidden=true; }
function renderRings() {
  const host = $("rings"), score = scoreValue((decisionWindow().advertised || {}).index);
  host.replaceChildren(scoreGauge(score,"宣传矛盾指数"));
  host.className="rings one";
  host.append(el("div","score-description",score==null?"尚无可核验的矛盾评分":"数值越高，当前宣传矛盾证据越强"));
}

/* ---------- 方面风险概览 ---------- */
function barRate(aspect) {
  return aspect.risk_index == null ? 0 : Number(aspect.risk_index);
}

function barValueText(aspect) {
  if (aspect.risk_index == null) return aspect.claim_count ? "未评估" : "无可比宣传";
  return Number(aspect.risk_index).toFixed(1);
}

function barRow(aspect, index) {
  const tone = toneOf(aspect);
  const value = scoreValue(aspect.risk_index), palette = probabilityPalette({valid:value!=null,estimate:value==null?null:value/100});
  const row = el("button", "bar-row" + ($("aspect").value === aspect.aspect ? " active" : ""));
  row.type = "button";
  row.setAttribute("aria-label", "筛选方面：" + aspect.aspect);
  row.style.setProperty("--tone", palette.color);

  const top = el("div", "bar-top");
  top.append(el("span", "bar-name", aspect.aspect), el("span", "bar-value", barValueText(aspect)));
  const track = el("div", "track");
  const fill = el("div", "fill");
  fill.style.width = "0%";
  fill.style.background = palette.color;
  track.append(fill);

  const foot = el("div", "bar-foot");
  const left = el("span", "lvl", aspect.contradiction_count ? aspect.contradiction_count + " 条明确矛盾" : "关联反馈");
  const st = aspect.evidence_structure || {};
  const right = el("span", "",
    aspect.possible_count ? aspect.possible_count + " 条关联" : "查看依据");
  foot.append(left, right);

  row.append(top, track, foot);
  row.addEventListener("click", () => {
    $("aspect").value = $("aspect").value === aspect.aspect ? "" : aspect.aspect;
    renderBars();
    renderCards();
    const panel = $("cards");
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "nearest" });
  });
  const target = aspect.risk_index == null ? 0 : Math.max(2, Math.min(100, Number(aspect.risk_index)));
  requestAnimationFrame(() => {
    setTimeout(() => { fill.style.width = target + "%"; }, reduced ? 0 : 60 + index * 45);
  });
  return row;
}

function renderBars() {
  const host = $("bars");
  host.replaceChildren();
  const aspects = advertisedAspects();
  const primary = aspects.filter((a) => a.risk_index != null);
  const others = aspects.filter((a) => a.risk_index == null);
  const rows = showZero ? aspects.slice() : primary.slice();
  rows.sort((a, b) => ((b.risk_index || -1) - (a.risk_index || -1)) ||
    (b.contradiction_count - a.contradiction_count));
  const high = aspects.filter((a) => a.risk_band === "high").length;
  const medium = aspects.filter((a) => a.risk_band === "medium").length;
  const low = aspects.filter((a) => a.risk_band === "low").length;
  $("barCount").textContent = primary.length + " 个已形成证据的维度";
  const toggle = $("toggleZero");
  toggle.hidden = others.length === 0;
  toggle.textContent = showZero ? "收起其他方面" : "其他方面 " + others.length;
  toggle.onclick = () => { showZero = !showZero; renderBars(); renderCards(); };

  if (!rows.length) {
    host.append(emptyBlock("没有可展示的方面", "当前样本还没有形成可核验的宣传矛盾证据；这不等于宣传已被证实。"));
    return;
  }
  rows.forEach((aspect, index) => host.append(barRow(aspect, index)));
}

/* ---------- 商品自身问题 ---------- */
function issueAssessment(row) {
  const clusterIds = new Set([row.cluster_id, ...(Array.isArray(row.cluster_ids) ? row.cluster_ids : [])].filter(Boolean).map(String));
  if (!clusterIds.size) return null;
  return ((currentEvidenceWindow() || {}).problems || []).find((problem) =>
    (problem.cluster_ids || []).some((id) => clusterIds.has(String(id)))) || null;
}
function openIssueReviews(row, assessment) {
  const box = el("div");
  const ids = [...new Set((row.review_ids || (assessment || {}).review_ids || []).map(String))];
  box.append(el("p", "basis", (row.aspect || "未归类方面") + " · 共 " + ids.length + " 条去重评论" + (row.severity_label ? " · 原问题程度：" + row.severity_label : "")));
  appendEvidenceAnalysis(box, assessment);
  box.append(el("p", "basis", "以下保留本问题可用的全部原文；同一评论可能支撑多个问题，各问题数量不可相加。日期、SKU 和来源缺失时须结合局限说明判断。"));
  const index = new Map((D.reviews || []).map((review) => [String(review.review_id), review]));
  const list = el("div");
  ids.forEach((id) => {
    const review = index.get(id);
    const card = el("div", "evidence"), top = el("div", "ev-top");
    if (review) top.append(starNode(review.star));
    const bits = ["评论 " + id];
    if (review && review.date) bits.push(review.date); if (review && review.sku) bits.push("SKU " + review.sku);
    top.append(el("span", "ev-meta", bits.join(" · ")));
    card.append(top, el("p", "ev-text", review ? review.content || "该评论没有可用正文。" : "当前报告未包含这条评论原文，请回到采集数据核查。")); list.append(card);
  });
  if (!ids.length) list.append(emptyBlock("暂无可用原文 ID", "未提供证据不代表问题已排除。"));
  box.append(list); openDialog(row.problem_name || row.name || "问题评论依据", box);
}

let extraAspect = "";
function issueQuoteText(row, assessment) {
  // Historical problem evidence can be a string or structured quote records.
  // Read text fields only; never stringify a record or its metadata as a quote.
  const text = (value, depth = 0) => {
    if (typeof value === "string") return value.trim();
    if (!value || typeof value !== "object" || depth > 3) return "";
    if (Array.isArray(value)) return value.map(item => text(item, depth + 1)).find(Boolean) || "";
    return ["quote", "text", "evidence", "content"].map(key => text(value[key], depth + 1)).find(Boolean) || "";
  };
  const source = evidenceState(assessment).source, evidence = source.evidence;
  const specific = Array.isArray(evidence) ? evidence.find(item => item && item.specific && text(item)) : null;
  const quote = text(specific) || text(row.evidence) || text(evidence);
  if (quote) return quote;
  const ids = new Set((row.review_ids || source.review_ids || []).map(String));
  return text((D.reviews || []).find(review => ids.has(String(review.review_id))));
}
function renderIssues() {
  const decisionExtra = decisionWindow().unadvertised || {}, hasDecisionProblems = Array.isArray(decisionExtra.problems);
  const originals = hasDecisionProblems ? [] : D.product_issues || [], panel = $("issuePanel"), host = $("issues");
  const engine = hasDecisionProblems ? decisionExtra.problems : ((currentEvidenceWindow()||{}).problems||[]).filter(row=>row.unadvertised);
  const represented = new Set(originals.flatMap(row=>[row.cluster_id,...(row.cluster_ids||[])].filter(Boolean).map(String)));
  const rows = originals.concat(engine.filter(row=>!(row.cluster_ids||[]).some(id=>represented.has(String(id)))).map(row=>({...row,problem_name:row.name||row.problem_name,review_count:row.review_count || (row.review_ids||[]).length,evidence:row.evidence||"",assessment:row})));
  const query = $("extraSearch").value.trim().toLowerCase();
  const visible = rows.filter(row=>(!extraAspect||row.aspect===extraAspect)&&(!query||(row.problem_name+" "+row.aspect+" "+issueQuoteText(row,row.assessment||issueAssessment(row))).toLowerCase().includes(query)));
  panel.hidden=false;host.replaceChildren();$("issueCount").textContent=extraAspect?extraAspect+" · "+visible.length+" 个问题（再次点击维度可取消筛选）":rows.length+" 个问题 · 展开查看评论原文";
  if(!visible.length){host.append(emptyBlock(rows.length?"没有匹配的问题":"尚未发现可归类的宣传外问题",rows.length?"调整搜索或维度筛选。":"可在采集范围中查看本次覆盖。"));return;}
  visible.forEach(row=>{
    const assessment=row.assessment || issueAssessment(row),state=evidenceState(assessment),card=el("div","issue-row");applyEvidenceTone(card,assessment);card.dataset.problem=row.problem_name;
    const content=el("div","issue-content"),top=el("div","issue-top");top.append(el("span","issue-name",row.problem_name),el("span","tag none",row.severity_label||state.source.impact_label||"待核查"));
    content.append(top,el("div","issue-meta",(row.aspect||"未归类")+" · "+(row.review_count || (row.review_ids||[]).length)+" 条支持评论"));
    if (state.source.scope_reason) content.append(el("div","issue-meta",state.source.scope_reason === "no_claim_in_aspect" ? "未找到对应宣传" : "宣传对应关系待核查"));
    const quote=issueQuoteText(row,assessment);
    if(quote)content.append(el("p","issue-quote","“"+quote+"”"));
    const p=probabilityState(assessment);content.append(el("p","estimate-brief",p.valid?"同类问题复现 · "+p.successes+" / "+p.total+" 条可比反馈"+(p.smallSample?" · 样本较少":""):"近期可比样本不足，暂未估计"));
    const circle=el("div","issue-signal");circle.append(evidenceVisual(assessment,true));
    const details=el("details","issue-details"),summary=el("summary","","查看分析与评论依据");details.append(summary);
    if(state.source.reason)details.append(el("p","finding",state.source.reason));
    if(state.source.attention_level==="safety")details.append(attentionBadge(assessment));
    const ids=[...new Set((row.review_ids||state.source.review_ids||[]).map(String))],index=new Map((D.reviews||[]).map(r=>[String(r.review_id),r]));
    ids.slice(0,3).forEach(id=>{const review=index.get(id),e=el("div","evidence"),meta=el("div","ev-top");if(review)meta.append(starNode(review.star));meta.append(el("span","ev-meta",[review&&review.date,review&&review.sku].filter(Boolean).join(" · ")));e.append(meta,el("p","ev-text",review?review.content||"暂无正文":"请在完整数据中核查评论 "+id));details.append(e);});
    const button=el("button","","查看全部 "+ids.length+" 条评论");button.type="button";button.addEventListener("click",()=>openIssueReviews(row,assessment));details.append(button);estimateBrief(details,assessment);
    card.append(content,circle,details);host.append(card);
  });
}

/* ---------- 方面卡片 ---------- */
function starNode(star) {
  const span = el("span", "stars");
  const value = typeof star === "number" ? Math.max(0, Math.min(5, Math.round(star))) : 0;
  span.textContent = value ? "★".repeat(value) + "☆".repeat(5 - value) : "";
  span.hidden = !value;
  return span;
}

function conflictRank(gap) {
  if (gap.gap_type === "potential_conflict") return 3;
  if (gap.gap_type === "partial_gap") return 2;
  if (gap.gap_type === "insufficient_evidence") return 1;
  return 0;
}

function tagLevel(gapType) {
  if (gapType === "potential_conflict") return "high";
  if (gapType === "partial_gap") return "medium";
  if (gapType === "insufficient_evidence") return "insufficient";
  return "low";
}

function gapStatusLabel(gapType) {
  if (gapType === "potential_conflict") return "明确矛盾";
  if (gapType === "partial_gap") return "可能矛盾";
  if (gapType === "insufficient_evidence") return "证据不足";
  return "未检出明确矛盾";
}

function claimBlock(gap, open) {
  const level = tagLevel(gap.gap_type);
  const wrap = el("details", "claim");
  wrap.open = !!open;
  const summary = el("summary");
  summary.append(
    el("span", "claim-title", gap.claim_text || "（未命名主张）"),
    el("span", "tag " + level, gapStatusLabel(gap.gap_type)),
    el("span", "claim-count", (gap.support_review_count || 0) + " 条")
  );
  const body = el("div", "claim-body");
  if (gap.claim_original_text) {
    body.append(el("div", "quote-label", "宣传原文"));
    body.append(el("blockquote", "promo", "“" + gap.claim_original_text + "”"));
  }
  if (gap.finding) body.append(el("p", "finding", gap.finding));


  const quotes = gap.quotes || [];
  if (quotes.length) {
    body.append(el("div", "ev-head", "相关评论证据"));
    quotes.slice(0, 2).forEach((quote) => {
      const card = el("div", "evidence");
      const top = el("div", "ev-top");
      top.append(starNode(quote.star));
      const bits = ["评论 " + quote.review_id];
      if (quote.date) bits.push(quote.date);
      if (quote.sku) bits.push("SKU " + quote.sku);
      top.append(el("span", "ev-meta", bits.join(" · ")));
      if (quote.evidence_status === "original_review_only") top.append(el("span", "tag insufficient", "补充原评论"));
      card.append(top, el("p", "ev-text", quote.evidence || quote.content || ""));
      body.append(card);
    });
    if (quotes.length > 2) {
      const more = el("button", "estimate-button", "查看全部 " + quotes.length + " 条评论依据"); more.type = "button";
      more.addEventListener("click", () => {
        const content = el("div");
        quotes.forEach(quote => {
          const card = el("div", "evidence"), top = el("div", "ev-top");
          top.append(starNode(quote.star), el("span", "ev-meta", [quote.review_id, quote.date, quote.sku].filter(Boolean).join(" · ")));
          card.append(top, el("p", "ev-text", quote.evidence || quote.content || "")); content.append(card);
        });
        openDialog("宣传对照 · 全部评论依据", content);
      });
      body.append(more);
    }
  } else {
    body.append(el("p", "basis", "没有可展示的评论原文，当前判断仅基于已匹配的问题簇。"));
  }
  wrap.append(summary, body);
  return wrap;
}

function aspectCard(aspect, index) {
  const tone = toneOf(aspect);
  const value = scoreValue(aspect.risk_index), palette = probabilityPalette({valid:value!=null,estimate:value==null?null:value/100});
  const detail = el("details", "aspect-card");
  detail.style.setProperty("--tone", palette.color);
  detail.style.setProperty("--tone-soft", palette.soft);
  detail.style.setProperty("--tone-ink", palette.ink);
  detail.style.animationDelay = Math.min(index * 45, 280) + "ms";
  detail.open = expanded && index === 0;

  const summary = el("summary");
  const idx = el("span", "idx", String(index + 1));
  const head = el("span", "head-text");
  const structure = aspect.evidence_structure || {};
  const sub = aspect.claim_count + " 条宣传 · " + aspect.contradiction_count + " 条明确矛盾";
  head.append(el("span", "name", aspect.aspect), el("small", "", sub));
  const tagText = aspect.risk_index == null ? "暂未形成宣传指数" : "宣传矛盾指数" + aspect.risk_band_label + "档";
  const tag = el("span", "tag " + tone, tagText);
  const support = el("span", "head-support");
  if (aspect.risk_index == null) {
    support.append(el("b", "", "—"), el("small", "", "证据" + aspect.evidence_sufficiency_label));
  } else {
    support.append(el("b", "", Number(aspect.risk_index).toFixed(1)),
      el("small", "", "宣传矛盾指数 · 证据" + aspect.evidence_sufficiency_label));
  }
  const chev = el("span", "chev");
  summary.append(idx, head, tag, support, chev);
  if (aspect.safety_alert) summary.append(el("span", "tag high", "安全相关"));

  const body = el("div", "aspect-body");
  const stats = el("div", "mini-stats");
  [["明确反驳宣传", aspect.contradiction_count + " 条"], ["相关负面反馈", aspect.possible_count + " 条"]].forEach(([label,value])=>{const box=el("div","mini");box.append(el("small","",label),el("b","",value));stats.append(box);});
  body.append(stats);
  const related = (aspect.gap_ids || []).map((id) => gaps.get(id)).filter(Boolean)
    .sort((a, b) => conflictRank(b) - conflictRank(a) || (b.support_review_count || 0) - (a.support_review_count || 0));
  if (!related.length) {
    body.append(el("p", "aspect-note", "该方面尚未形成可核验的落差判断。"));
  } else {
    related.forEach((gap, gapIndex) => {
      body.append(claimBlock(gap, gapIndex === 0 && conflictRank(gap) >= 2));
    });
  }
  detail.append(summary, body);
  return detail;
}

function aspectVisible(aspect) {
  if (aspect.risk_index != null) return true;
  if ($("aspect").value && $("aspect").value === aspect.aspect) return true;
  return showZero || $("risk").value === "none";
}

function corpusOf(aspect) {
  const parts = [aspect.aspect];
  (aspect.gap_ids || []).forEach((id) => {
    const gap = gaps.get(id);
    if (!gap) return;
    parts.push(gap.claim_text || "", gap.claim_original_text || "", gap.finding || "", gap.confidence_basis || "");
    (gap.quotes || []).forEach((quote) => parts.push(quote.evidence || ""));
  });
  return parts.join(" ").toLowerCase();
}

function matches(aspect) {
  const level = $("risk").value;
  if (level && (aspect.risk_band || "none") !== level) return false;
  if ($("aspect").value && aspect.aspect !== $("aspect").value) return false;
  const query = $("search").value.trim().toLowerCase();
  return !query || corpusOf(aspect).includes(query);
}

function sortRows(rows) {
  const mode = $("sort").value;
  return rows.slice().sort((a, b) => {
    if (mode === "evidence") {
      return (b.contradiction_count - a.contradiction_count) || (b.possible_count - a.possible_count) ||
        ((b.risk_index || -1) - (a.risk_index || -1));
    }
    if (mode === "partial") {
      return (b.possible_count - a.possible_count) || (b.contradiction_count - a.contradiction_count);
    }
    return ((b.risk_index == null ? -1 : b.risk_index) - (a.risk_index == null ? -1 : a.risk_index)) ||
      (b.contradiction_count - a.contradiction_count);
  });
}

function renderCards() {
  const host = $("cards");
  host.replaceChildren();
  const rows = sortRows(advertisedAspects().filter(aspectVisible).filter(matches));
  $("resultCount").textContent = rows.length
    ? rows.length + " 个方面；展开可查看宣传原文与评论证据"
    : "没有匹配的方面";
  if (!rows.length) {
    host.append(emptyBlock("没有匹配结果", "调整搜索词或筛选条件再试。"));
    return;
  }
  rows.forEach((aspect, index) => host.append(aspectCard(aspect, index)));
}

/* ---------- 筛选与弹窗 ---------- */
const aspectSelect = $("aspect");
aspectSelect.append(new Option("全部方面", ""));
advertisedAspects().forEach((aspect) => aspectSelect.append(new Option(aspect.aspect, aspect.aspect)));
["aspect", "risk", "sort"].forEach((id) => $(id).addEventListener("change", () => {
  if (id === "aspect") renderBars();
  renderCards();
}));
$("search").addEventListener("input", renderCards);
$("extraSearch").addEventListener("input", renderIssues);
$("collapseButton").addEventListener("click", () => {
  expanded = !expanded;
  document.querySelectorAll(".aspect-card").forEach((node) => { node.open = expanded; });
  $("collapseButton").textContent = expanded ? "收起全部" : "展开全部";
});

function openDialog(title, content) {
  $("dialogTitle").textContent = title;
  $("dialogBody").replaceChildren(content);
  $("detailDialog").showModal();
}
$("closeDialog").addEventListener("click", () => $("detailDialog").close());
$("detailDialog").addEventListener("click", (event) => {
  if (event.target === $("detailDialog")) $("detailDialog").close();
});

$("methodButton").addEventListener("click", () => {
  const box=el("div");
  [["阅读顺序","先看采集概况和总览，再分别核查宣传落差与宣传外问题。指数越高，越值得关注；空值表示待评估。"],
   ["两个指数分别计算","宣传矛盾指数依据已核对原文、去重的明确矛盾证据；额外风险指数为最主要额外具体问题的条件复现估计乘以100。包括未找到对应宣传、以及宣传直接对应关系尚待核查的问题，分别标注。实际影响与安全线索单独提醒。综合关注指数保留两侧的较高值，不做平均。"],
   ["概率的含义","同类问题复现概率：在已完成问题抽取的同商品、同规格、近期可分析低星反馈中，后续同类反馈再次提及该具体问题的估计。不是购买后故障率。每个摘要选择一个主要问题，不汇总不同问题来凑数。"],
   ["模型与不确定性","使用Jeffreys先验 Beta(0.5,0.5)，后验 Beta(k+0.5,n−k+0.5)，估计为(k+0.5)/(n+1)。95%模型参数可信区间不包含选择、采集与分类偏差；小样本可能有很宽区间。每张概率卡的“查看估计依据”可查看区间、规格与样本。"],
   ["证据范围","去重评论不代表不同买家。零记录不等于商品没有问题；不同规格不混合计算，失败或未完成抽取不充当无问题样本。"],
   ["采集状态",notices.join(" ") || "本次数据状态完整。"]].forEach(([title,text])=>box.append(el("h3","",title),el("p","",text)));
  const method=DECISION.method || {};[["宣传矛盾指数计算",method.advertised_index],["额外风险指数计算",method.unadvertised_index]].forEach(([name,value])=>{if(value)box.append(el("h3","",name),el("p","",value));});
  openDialog("分析方法与采集范围",box);
});

$("productButton").addEventListener("click", () => {
  const box = el("div");
  const product = D.product || {};
  box.append(el("h3", "", product.title || D.title || "商品宣传信息"));
  const table = el("table");
  const labels = { id: "商品 ID", title: "标题", subtitle: "副标题", price_text: "价格", price: "价格", attributes: "商品属性", props: "商品属性", sku_count: "SKU 数量", skus: "SKU", brand: "品牌", category: "品类" };
  Object.entries(product).forEach(([key, value]) => {
    if (key === "promotional_claims" || key === "images") return;
    const row = el("tr");
    row.append(el("th", "", labels[key] || key), el("td", "", typeof value === "object" ? JSON.stringify(value, null, 2) : value));
    table.append(row);
  });
  box.append(table);
  box.append(el("h3", "", "提取后的宣传主张"));
  const list = el("ol");
  (D.claims || []).forEach((claim) => {
    const item = el("li", "", claim.original_text || claim.text);
    item.append(el("small", "", claim.aspect + " · " + claim.source + (claim.source_image ? " · " + claim.source_image : "")));
    list.append(item);
  });
  if (!(D.claims || []).length) list.append(el("li", "", "未提取到宣传主张。"));
  box.append(list);
  const raw = el("details");
  raw.append(el("summary", "", "原始宣传文本（" + ((product.promotional_claims || []).length) + " 条）"));
  const rawList = el("ol");
  (product.promotional_claims || []).forEach((row) => {
    const item = el("li", "", row.text);
    item.append(el("small", "", row.source + (row.source_image ? " · " + row.source_image : "")));
    rawList.append(item);
  });
  raw.append(rawList);
  box.append(raw);
  openDialog("商品宣传信息", box);
});

/* ---------- 未对应商品宣传的负面评价警示 ---------- */
function renderRiskAlert() {
  const host=$("riskAlert"),assessment=decisionAssessment("unadvertised"),state=evidenceState(assessment);
  const hasEvidence=(countOrNull(state.source.review_count)||0)>0||(state.source.review_ids||[]).length>0;
  host.replaceChildren();host.hidden=!state.known||state.source.attention_level==="none"&&!hasEvidence;if(host.hidden)return;
  host.className="risk-alert is-"+state.tone;applyEvidenceTone(host,assessment);
  if(state.source.attention_level==="safety")applyProbabilityPalette(host,probabilityPalette({valid:true,estimate:1}));
  host.append(el("span","badge",state.source.attention_level==="safety"?"安全线索":"额外关注"));
  const prefix=state.source.scope_reason === "not_established_ad_contradiction" ? "额外需关注的体验反馈：" : "宣传未覆盖的反馈：";
  const body=el("div","body");body.append(el("strong","warning-title",prefix+(state.source.name||state.source.problem_name||"有待核查的问题")),el("div","line",evidenceCountLine(assessment)));host.append(body);
  const jump=el("button","warning-link","查看问题依据");jump.type="button";jump.addEventListener("click",()=>$("extraSection").scrollIntoView({behavior:reduced?"auto":"smooth",block:"start"}));host.append(jump);
}
$("sampleDenominator").addEventListener("change", () => {
  sampleDenominator = $("sampleDenominator").value === "eligible" ? "eligible" : "scanned";
  renderSampleSummary(); reportHeight();
});
const windowSelector = $("evidenceWindow");
const availableWindows = Array.isArray(IE.window_options) ? IE.window_options.filter((days) => [7, 30, 90].includes(Number(days))).map(Number) : [7, 30, 90];
[...new Set([...availableWindows, Number(evidenceWindowDays)])].sort((a, b) => a - b).forEach((days) => windowSelector.append(new Option("近" + days + "天", String(days))));
windowSelector.value = evidenceWindowDays;
windowSelector.addEventListener("change", () => {
  evidenceWindowDays = ["7", "30", "90"].includes(windowSelector.value) ? windowSelector.value : "30";
  if(extraAspect&&!((decisionWindow().unadvertised||{}).aspects||[]).some(row=>row.aspect===extraAspect))extraAspect="";
  renderEvidenceSummary(); renderEvidenceProblems(); renderRings(); renderBars(); renderCards(); renderIssues(); renderRiskAlert(); reportHeight();
});

renderEvidenceSummary();
renderEvidenceProblems();
renderSampleSummary();
renderRings();
renderRingAspects();
renderBars();
renderCards();
renderIssues();
renderRiskAlert();

// 应用内预览通过 iframe 嵌入：把内容高度上报给父窗口，整页展开，避免裁切。
function reportHeight() {
  if (!window.parent || window.parent === window) return;
  try {
    window.parent.postMessage({ type: "report-height", height: Math.ceil(document.documentElement.scrollHeight) }, "*");
  } catch (err) {}
}
if (typeof window.addEventListener === "function") {
  window.addEventListener("load", reportHeight);
  window.addEventListener("resize", reportHeight);
}
if (window.ResizeObserver) new ResizeObserver(reportHeight).observe(document.body);
</script>
</body>
</html>"""
