"""可视化报告的 HTML 模板。

单独成文件便于审阅与修改；模板只做渲染，不读取文件、不访问网络。
占位符：__REPORT_DATA__ / __REPORT_TITLE__ / __VIEW_MODE__。
"""

HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>__REPORT_TITLE__</title>
<style>
:root{
  --ink:#152230;--muted:#6b7c8c;--line:#e5eaf0;--bg:#f3f6f9;--panel:#ffffff;
  --brand:#0f7681;--brand-soft:#e6f3f4;--brand-ink:#155e67;
  --high:#d93f3f;--high-soft:#fdeaea;--high-ink:#a02323;
  --medium:#d98500;--medium-soft:#fff4e0;--medium-ink:#8a5400;
  --low:#2f9e63;--low-soft:#e8f7ef;--low-ink:#1b6b42;
  --insufficient:#6f8296;--insufficient-soft:#eef3f8;--insufficient-ink:#4d6073;
  --none:#8a97a6;--none-soft:#f1f4f7;--none-ink:#5f6d7c;
  --violet:#6f5bd4;--violet-soft:#efecff;
  --shadow:0 1px 2px rgba(19,40,65,.05),0 16px 32px -24px rgba(19,40,65,.45);
  --radius:16px;
}
*{box-sizing:border-box}
html,body{min-height:100%}
body{
  margin:0;font:15px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  color:var(--ink);overflow:auto;
  background:radial-gradient(1100px 460px at 12% -8%,#eaf5f6 0%,rgba(234,245,246,0) 62%),var(--bg);
}
[hidden]{display:none!important}
button,input,select{font:inherit;color:inherit}
button,select{cursor:pointer}
button{border:1px solid var(--line);background:#fff;border-radius:10px;padding:7px 12px;transition:border-color .18s,background .18s,transform .18s}
button:hover{border-color:#c6d5de;background:#f8fbfc}
button:active{transform:translateY(1px)}
button:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #8fd0d6;outline-offset:2px}
h1,h2,h3{margin:0}
.shell{max-width:1500px;margin:0 auto;padding:22px 26px 24px;min-height:100vh;display:flex;flex-direction:column;gap:14px}

/* ---------- 顶部 ---------- */
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}
.eyebrow{font-size:10.5px;letter-spacing:1.8px;font-weight:750;color:var(--brand);text-transform:uppercase}
.title{font-size:25px;line-height:1.32;margin:4px 0 3px;max-width:960px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.subtitle{font-size:12.5px;color:var(--muted)}
.header-actions{display:flex;gap:8px;flex-shrink:0;padding-top:4px}
.header-actions button{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;padding:8px 13px;box-shadow:0 1px 1px rgba(19,40,65,.03)}
.header-actions svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}

.notice{padding:9px 14px;border-radius:10px;background:#fff5e2;color:#8b5f14;font-size:12px;border:1px solid #f4e3c2}

/* ---------- 指标卡 ---------- */
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.metric{--tone:var(--brand);--tone-soft:var(--brand-soft);display:flex;align-items:center;gap:13px;
  padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:#fff;box-shadow:var(--shadow);
  animation:rise .5s cubic-bezier(.2,.8,.2,1) both}
.metric .icon{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;background:var(--tone-soft);color:var(--tone);flex:none}
.metric .icon svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.metric .meta{display:flex;flex-direction:column;gap:1px;min-width:0}
.metric .label{font-size:13px;color:var(--muted)}
.metric b{font-size:31px;font-weight:680;letter-spacing:-.8px;line-height:1.15;font-variant-numeric:tabular-nums}
.metric small{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---------- 工作区 ---------- */
.workspace{display:grid;grid-template-columns:minmax(318px,.82fr) minmax(520px,1.68fr);gap:14px;align-items:start}
.side{display:flex;flex-direction:column;gap:14px;scrollbar-width:thin;scrollbar-color:#c9d6df transparent}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);display:flex;flex-direction:column;min-height:0;overflow:hidden;box-shadow:var(--shadow)}
/* 左栏在 sticky 容器里有高度上限，面板必须保持自身高度，超出交给 .side 滚动，
   否则 flex 会把面板压扁、overflow:hidden 再把圆环裁掉。 */
.side>.panel{flex:0 0 auto}
.panel.grow{flex:0 1 auto}
.panel-head{flex:none;padding:15px 17px 10px;border-bottom:1px solid #eef2f6}
.panel-head h2{font-size:17px;letter-spacing:-.2px}
.panel-head .desc{font-size:12px;color:var(--muted);margin-top:2px}
.head-split{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.inline-button{font-size:12px;padding:5px 10px;border-radius:8px;color:var(--muted);background:#fbfcfd;white-space:nowrap;flex:none}
.scroll{overflow:auto;scrollbar-width:thin;scrollbar-color:#c9d6df transparent;overscroll-behavior:contain}
.panel-foot{padding:9px 15px 12px;font-size:11.5px;line-height:1.55;color:var(--muted);border-top:1px dashed #eef2f6}

/* ---------- 购物关注概览 ---------- */
.shopping-summary{padding:15px 17px 13px}
.shopping-summary .attention-title{font-size:20px;line-height:1.4;color:#694a20;font-weight:650;margin-bottom:4px}
.shopping-summary .attention-description{font-size:12px;color:var(--muted);margin:0 0 13px}
.shopping-counts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}
.shopping-count{padding:9px 8px;background:#f7fafc;border:1px solid #edf1f5;border-radius:10px;display:flex;flex-direction:column}
.shopping-count b{font-size:23px;line-height:1.25;font-weight:670;font-variant-numeric:tabular-nums}
.shopping-count span{font-size:11px;color:var(--muted);margin-top:3px}
.safety-note{padding:9px 11px;margin:11px 0 0;border:1px solid #f4d4ce;border-radius:9px;color:#a03d2c;background:#fff4f0;font-size:12px}

/* ---------- 整体宣传落差风险 ---------- */
.overall-risk{padding:14px 17px 15px;gap:9px}
.risk-headline{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.risk-badge{display:inline-flex;align-items:center;gap:8px;font-size:16px;font-weight:720;padding:7px 15px;border-radius:999px;border:1.5px solid;letter-spacing:.5px}
.risk-badge .dot{width:10px;height:10px;border-radius:50%;background:currentColor}
.risk-high{color:#a03125;border-color:#efc4bb;background:#fff1ed}
.risk-medium{color:#8a5a10;border-color:#eed9ac;background:#fff7e4}
.risk-low{color:#2e6b3f;border-color:#c4dcc8;background:#eef7ef}
.risk-desc{font-size:12px;color:var(--muted);line-height:1.65}
.hidden-risks{margin-top:2px;display:flex;flex-direction:column;gap:6px}
.hidden-risks-title{font-size:13px;font-weight:680;color:#694a20}
.hidden-risk-row{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:7px 11px;border:1px solid #f0e2c8;border-radius:10px;background:#fffaf0;font-size:12.5px;color:#5c4a22}
.hidden-risk-row b{font-weight:680}
.hidden-risk-row .cnt{color:#a03125;font-weight:680}
.hidden-risk-empty{font-size:12px;color:var(--muted)}
.risk-overview{display:grid;grid-template-columns:minmax(160px,190px) minmax(0,1fr);gap:16px;align-items:center;margin:4px 0 2px}
.risk-big{display:flex;flex-direction:column;gap:1px}
.risk-big b{font-size:40px;font-weight:730;letter-spacing:-1.2px;line-height:1.02;font-variant-numeric:tabular-nums}
.risk-big small{font-size:11.5px;color:var(--muted);line-height:1.5}
.risk-dist{display:flex;flex-direction:column;gap:7px;min-width:0}
.risk-dist-bar{display:flex;height:10px;border-radius:999px;overflow:hidden;background:#eef2f6}
.risk-dist-bar span{display:block;height:100%}
.seg-high{background:#d64533}.seg-medium{background:#e5972f}.seg-low{background:#4c9e57}
.risk-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted)}
.risk-legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.risk-detail{display:flex;flex-wrap:wrap;gap:6px}
.risk-chip{font-size:11.5px;padding:3px 9px;border-radius:999px;border:1px solid #e6edf3;background:#fbfdfe;color:#415463}
.risk-chip b{font-variant-numeric:tabular-nums}
@media(max-width:720px){.risk-overview{grid-template-columns:1fr}}
.promo-overview{border-top:1px solid #edf1f5;margin-top:12px;padding-top:10px;color:var(--muted);font-size:12px}
.promo-overview>summary{cursor:pointer;font-weight:600;color:#526778}
.promo-overview p{margin:7px 0 0;font-size:12px}
.empty{padding:26px 18px;text-align:center;font-size:12px;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:13px;margin-bottom:3px}

/* ---------- 方面条 ---------- */
.bars{padding:7px 10px 12px;display:flex;flex-direction:column;gap:1px}
.issue-panel{margin-top:14px}
.issue-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;padding:12px 16px 16px}
.issue-row{display:flex;flex-direction:column;gap:5px;padding:10px 12px;border:1px solid #e6edf3;border-radius:11px;background:#fbfdfe;cursor:pointer;transition:border-color .18s,background .18s}
.issue-row:hover{border-color:#c6d5de;background:#f7fbfc}
.issue-row:focus-visible{outline:3px solid #8fd0d6;outline-offset:2px}
.issue-top{display:flex;align-items:center;gap:8px;justify-content:space-between}
.issue-name{font-weight:600;font-size:13.5px;color:#22313f;line-height:1.35}
.issue-meta{font-size:11.5px;color:#6b7d8d}
.issue-quote{font-size:12px;color:#55697a;line-height:1.55;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(max-width:900px){.issue-list{grid-template-columns:1fr}}
.bar-row{--tone:var(--low);display:block;width:100%;text-align:left;border:0;background:transparent;border-radius:11px;padding:9px 10px;transition:background .18s}
.bar-row:hover{background:#f7fafc}
.bar-row.active{background:var(--brand-soft)}
.bar-top{display:flex;align-items:center;gap:8px}
.bar-name{flex:1;min-width:0;font-size:14.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-value{font-size:14.5px;font-weight:680;color:var(--tone);font-variant-numeric:tabular-nums}
.track{height:7px;border-radius:99px;background:#eef2f6;overflow:hidden;margin:6px 0 4px}
.fill{height:100%;width:0;border-radius:99px;background:var(--tone);transition:width .9s cubic-bezier(.22,.9,.28,1)}
.bar-foot{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;color:var(--muted)}
.bar-foot .lvl{color:var(--tone);font-weight:600}

/* ---------- 工具栏 ---------- */
.toolbar{display:grid;grid-template-columns:minmax(140px,1.5fr) repeat(3,minmax(96px,1fr));gap:8px;padding:10px 16px;border-bottom:1px solid #eef2f6;background:#fcfdfe}
.toolbar input,.toolbar select{border:1px solid var(--line);border-radius:9px;padding:7px 9px;background:#fff;font-size:13px;min-width:0}
.toolbar input::placeholder{color:#9aa8b5}

/* ---------- 方面卡片 ---------- */
.cards{padding:11px 14px 24px;display:flex;flex-direction:column;gap:10px}
.aspect-card{flex:0 0 auto;--tone:var(--none);--tone-soft:var(--none-soft);--tone-ink:var(--none-ink);border:1px solid var(--line);border-radius:14px;background:#fff;overflow:hidden;
  animation:rise .45s cubic-bezier(.2,.8,.2,1) both}
.aspect-card[open]{border-color:#d7e3ea;box-shadow:0 1px 2px rgba(19,40,65,.04),0 18px 34px -30px rgba(19,40,65,.5)}
.aspect-card>summary{display:flex;align-items:center;gap:11px;padding:11px 13px;cursor:pointer;list-style:none}
.aspect-card>summary::-webkit-details-marker{display:none}
.aspect-card>summary:hover{background:#fbfdfe}
.idx{width:26px;height:26px;border-radius:50%;background:var(--tone);color:#fff;font-size:12px;font-weight:700;display:grid;place-items:center;flex:none;font-variant-numeric:tabular-nums}
.head-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.head-text .name{font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head-text small{font-size:11.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head-support{display:flex;flex-direction:column;align-items:flex-end;gap:0;flex:none;min-width:86px}
.head-support b{font-size:13.5px;font-weight:650;font-variant-numeric:tabular-nums}
.head-support small{font-size:11px;color:var(--muted)}
.chev{width:7px;height:7px;border-right:1.8px solid #9fb0bf;border-bottom:1.8px solid #9fb0bf;transform:rotate(45deg);transition:transform .25s;margin:0 3px 4px 2px;flex:none}
.aspect-card[open] .chev{transform:rotate(225deg);margin-bottom:-2px}
.tag{display:inline-flex;align-items:center;border-radius:99px;padding:3px 10px;font-size:11.5px;font-weight:650;white-space:nowrap;line-height:1.5}
.tag.brand{background:var(--brand-soft);color:var(--brand-ink)}
.tag.high{background:var(--high-soft);color:var(--high-ink)}
.tag.medium{background:var(--medium-soft);color:var(--medium-ink)}
.tag.low{background:var(--low-soft);color:var(--low-ink)}
.tag.insufficient{background:var(--insufficient-soft);color:var(--insufficient-ink)}
.tag.none{background:var(--none-soft);color:var(--none-ink)}

.aspect-body{padding:2px 13px 13px;border-top:1px dashed #eef2f6}
.problem-list{display:flex;flex-direction:column;gap:7px;margin:8px 0 10px}
.problem{border:1px solid #e6edf3;border-radius:10px;background:#fbfdfe;overflow:hidden}
.problem>summary,.review-list>summary{display:flex;align-items:center;justify-content:space-between;gap:10px;cursor:pointer;padding:9px 11px;font-weight:600;font-size:13px}
.problem>summary small{font-size:11px;color:var(--muted);font-weight:400;white-space:nowrap}
.problem-body{padding:0 11px 11px}.problem-body p{margin:0 0 6px;font-size:12px;color:#536778}
.review-list{margin:9px 0;border:1px solid #e5ecf2;border-radius:10px;padding-bottom:3px}
.review-list .evidence{margin:7px 10px}.review-list .basis{margin:8px 11px}
.promo-comparison{margin-top:12px;border-top:1px solid #e6edf3;padding-top:9px}
.promo-comparison>summary{display:flex;gap:8px;align-items:center;justify-content:space-between;cursor:pointer;font-size:12px;color:#647789;padding:5px 0}
.promo-comparison>summary b{font-size:12px;font-weight:600}.promo-comparison .mini-stats{grid-template-columns:repeat(3,1fr)}
.promo-body{padding-top:3px}
.mini-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0 8px}
.mini{background:#f8fbfc;border:1px solid #eef2f6;border-radius:9px;padding:6px 8px;display:flex;flex-direction:column;gap:0}
.mini small{font-size:10.5px;color:var(--muted);line-height:1.5}
.mini b{font-size:14px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.35}
.aspect-note{font-size:12px;color:var(--muted);margin:2px 0 8px}

.claim{border:1px solid #eef2f6;border-radius:11px;background:#fcfdfe;margin-top:8px;overflow:hidden}
.claim>summary{display:flex;align-items:center;gap:8px;padding:8px 11px;cursor:pointer;list-style:none;background:#fbfdfe}
.claim>summary::-webkit-details-marker{display:none}
.claim>summary:hover{background:#f6fafb}
.claim-title{flex:1;min-width:0;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.claim-count{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.claim-body{padding:0 11px 11px}
.quote-label{font-size:11px;color:var(--muted);margin:8px 0 3px}
.promo{margin:0;padding:9px 12px;border-left:3px solid var(--brand);background:var(--brand-soft);border-radius:0 9px 9px 0;font-size:13px;color:#205c64;line-height:1.65;word-break:break-word}
.finding{font-size:12px;margin:8px 0 0;line-height:1.65;color:#283a4a}
.basis{font-size:10.5px;color:var(--muted);margin:5px 0 0;line-height:1.6}
.ev-head{display:flex;align-items:center;gap:7px;font-size:10.5px;color:var(--muted);margin:10px 0 5px}
.ev-head:before{content:"";width:3px;height:11px;border-radius:2px;background:#c9d8e1}
.evidence{border:1px solid #eef2f6;border-radius:9px;background:#fff;padding:8px 10px;margin-top:6px}
.ev-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.stars{color:#e3a008;letter-spacing:1.5px;font-size:12px;line-height:1}
.ev-meta{font-size:10px;color:var(--muted)}
.ev-text{margin:6px 0 0;font-size:13px;line-height:1.66;word-break:break-word;color:#2c3d4d}
.empty-list{padding:34px 18px;text-align:center;color:var(--muted);font-size:12px}
.empty-list b{display:block;color:var(--ink);font-size:13px;margin-bottom:4px}

/* ---------- 底栏与弹窗 ---------- */
footer{display:flex;justify-content:space-between;gap:16px;font-size:11.5px;color:var(--muted);padding:0 2px}
dialog{border:1px solid var(--line);border-radius:16px;padding:0;max-width:820px;width:calc(100vw - 40px);max-height:82vh;box-shadow:0 40px 90px -30px rgba(15,35,55,.5)}
dialog::backdrop{background:rgba(16,32,48,.42);backdrop-filter:blur(2px)}
.dialog-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#fff}
.dialog-head h2{font-size:16px}
.dialog-body{padding:16px 18px 22px;overflow:auto;max-height:calc(82vh - 60px)}
.dialog-body h3{font-size:14px;margin:14px 0 5px}
.dialog-body h3:first-child{margin-top:0}
.dialog-body p{font-size:13px;line-height:1.7;color:#2c3d4d;margin:4px 0}
.dialog-body table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
.dialog-body th,.dialog-body td{text-align:left;padding:7px 9px;border-bottom:1px solid #eef2f6;vertical-align:top;word-break:break-word}
.dialog-body th{width:110px;color:var(--muted);font-weight:550;white-space:nowrap}
.dialog-body ol,.dialog-body ul{margin:6px 0;padding-left:20px;font-size:12px;line-height:1.7}
.dialog-body li{padding:2px 0;word-break:break-word}
.dialog-body small{display:block;color:var(--muted);font-size:10.5px}
.formula{background:#f7fafc;border:1px solid #eef2f6;border-radius:10px;padding:10px 12px;font-size:12px;line-height:1.8}
.formula code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;color:#0f5f68}

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
  .cards,.bars{max-height:none;overflow:visible}
}
@media (max-width:720px){
  .shell{padding:16px}
  .metrics{grid-template-columns:1fr}
  .toolbar{grid-template-columns:1fr 1fr}
  .mini-stats{grid-template-columns:1fr 1fr}
  footer{flex-direction:column;gap:3px}
}
</style>
</head>
<body>
<main class="shell">
  <header class="top">
    <div style="min-width:0">
      <div class="eyebrow">购物问题与证据</div>
      <h1 class="title" id="title"></h1>
      <div class="subtitle" id="subtitle"></div>
    </div>
    <div class="header-actions">
      <button id="productButton" type="button"><svg viewBox="0 0 24 24"><path d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h4"/></svg>商品宣传信息</button>
      <button id="methodButton" type="button"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v5h1"/></svg>分析口径</button>
    </div>
  </header>

  <div id="notice" class="notice" hidden></div>
  <section id="metrics" class="metrics" aria-label="样本与证据概览"></section>

  <section id="overallRisk" class="panel overall-risk" hidden aria-label="整体宣传落差风险"></section>

  <div class="workspace">
    <aside class="side">
      <section class="panel">
        <div class="panel-head">
          <h2>购物关注概览</h2>
          <div class="desc">先看评论中实际出现的问题，再单独对照宣传承诺</div>
        </div>
        <div id="shoppingOverview" class="shopping-summary"></div>
        <div class="panel-foot">关注提示归纳本次样本的负面问题；不是商品整体安全评级，也不是购买后出问题的概率。</div>
      </section>

      <section class="panel grow">
        <div class="panel-head head-split">
          <div>
            <h2>各方面负面问题</h2>
            <div class="desc" id="barCount"></div>
          </div>
          <button id="toggleZero" class="inline-button" type="button" hidden></button>
        </div>
        <div id="bars" class="bars scroll"></div>
      </section>
    </aside>

    <section class="panel grow">
      <div class="panel-head head-split">
        <div>
          <h2>购物问题与证据</h2>
          <div class="desc" id="resultCount"></div>
        </div>
        <button id="collapseButton" class="inline-button" type="button">收起全部</button>
      </div>
      <div class="toolbar">
        <input id="search" type="search" placeholder="搜索问题、方面或评论原文" aria-label="搜索报告">
        <select id="aspect" aria-label="选择方面"></select>
        <select id="risk" aria-label="筛选购物关注提示">
          <option value="">全部关注提示</option>
          <option value="safety">安全相关</option>
          <option value="concentrated">集中出现</option>
          <option value="repeated">多条提及</option>
          <option value="single">单条线索</option>
          <option value="none">未见负面记录</option>
          <option value="unclassified">待核查低星正文</option>
        </select>
        <select id="sort" aria-label="排序">
          <option value="shopping">按购物关注提示</option>
          <option value="negative">按负面评论数</option>
          <option value="promo">按宣传矛盾指数</option>
        </select>
      </div>
      <div id="cards" class="cards scroll"></div>
    </section>
  </div>

  <footer>
    <span>比例只描述本次扫描与低星样本，不代表实际购买概率。未宣传的问题同样展示；各方面评论数不可直接相加。</span>
    <span>__VIEW_MODE__ · 可折叠、筛选与搜索</span>
  </footer>
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
const ATTENTION_ORDER = { none: 0, single: 1, repeated: 2, concentrated: 3, safety: 4 };
const ATTENTION_LABELS = { none: "暂无已归类负面证据", single: "个别负面反馈", repeated: "多条负面反馈", concentrated: "重点关注", safety: "安全相关线索" };
const gaps = new Map((D.gaps || []).map((g) => [g.claim_id, g]));
const reviewIndex = new Map((D.reviews || []).map((review) => [String(review.review_id), review]));
const O = D.overview || {};
const S = D.sampling || {};
const scannedCount = Number(O.scanned_review_count == null ? S.scanned_review_count : O.scanned_review_count) || 0;
const lowStarCount = Number(O.low_star_review_count == null ? S.low_star_review_count : O.low_star_review_count) || 0;
const effectiveCount = Number(O.valid_review_count == null ? S.effective_negative_review_count : O.valid_review_count) || 0;
const reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
let expanded = true;
let showZero = false;

function shoppingOf(aspect) {
  const shopping = aspect.shopping || {};
  const ids = Array.from(new Set((shopping.review_ids || aspect.negative_review_ids || []).map(String)));
  const reportedCount = shopping.negative_count == null ? aspect.negative_count : shopping.negative_count;
  const count = reportedCount == null ? ids.length : Math.max(0, Number(reportedCount) || 0);
  const level = shopping.attention_level || (count >= 2 ? "repeated" : count ? "single" : "none");
  const rate = (value, denominator) => value != null ? value : denominator > 0 ? count / denominator : null;
  const problems = (shopping.problems || aspect.problems || []).map((problem) => ({
    name: problem.name || problem.problem_name || "未命名问题", description: problem.description || problem.evidence || "",
    review_ids: (problem.review_ids || []).map(String)
  }));
  const evidence = new Map((shopping.evidence || []).map((review) => [String(review.review_id), review]));
  ids.forEach((id) => { if (!evidence.has(id) && reviewIndex.has(id)) evidence.set(id, reviewIndex.get(id)); });
  return { ...shopping, negative_count: count, attention_level: level,
    attention_label: shopping.attention_label || ATTENTION_LABELS[level], review_ids: ids, problems,
    evidence: Array.from(evidence.values()), scanned_rate: rate(shopping.scanned_rate, scannedCount),
    low_star_share: rate(shopping.low_star_share, lowStarCount), effective_share: rate(shopping.effective_share, effectiveCount) };
}
function shoppingTone(aspect) {
  const level = shoppingOf(aspect).attention_level;
  return level === "safety" || level === "concentrated" ? "high" : level === "repeated" ? "medium" : level === "single" ? "brand" : "none";
}
const shoppingAspects = (D.aspects || []).filter((aspect) => shoppingOf(aspect).negative_count > 0);
const coveredIds = new Set(shoppingAspects.flatMap((aspect) => shoppingOf(aspect).review_ids));
const shoppingSummary = (D.summary || {}).shopping || O.shopping || D.shopping || {};
const unclassifiedRows = (D.reviews || []).filter((review) => review.valid_for_analysis && !coveredIds.has(String(review.review_id)));
const unclassifiedCount = Number(shoppingSummary.unclassified_effective_count == null ? Math.max(0, effectiveCount - coveredIds.size) : shoppingSummary.unclassified_effective_count) || 0;
const shoppingAspectCount = Number(shoppingSummary.aspect_count == null ? shoppingAspects.length : shoppingSummary.aspect_count) || 0;
const repeatedAspectCount = Number(shoppingSummary.repeated_aspect_count == null ? shoppingAspects.filter((aspect) => shoppingOf(aspect).negative_count >= 2).length : shoppingSummary.repeated_aspect_count) || 0;

const fmtRate = (value, digits) => (value == null ? "未评估" : (value * 100).toFixed(digits == null ? 2 : digits) + "%");
const fmtCI = (ci) => (ci ? (ci[0] * 100).toFixed(2) + "%–" + (ci[1] * 100).toFixed(2) + "%" : "—");
const fmtCount = (value) => (value == null ? "0" : String(value));

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
if (notices.length) {
  $("notice").hidden = false;
  $("notice").textContent = notices.join(" ");
}

const ICONS = {
  doc: '<path d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h4"/>',
  alert: '<path d="M12 3.6 21 19a1 1 0 0 1-.9 1.5H3.9A1 1 0 0 1 3 19Z"/><path d="M12 9v5"/><path d="M12 17.2h.01"/>',
  users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3 19c0-3 2.7-5 6-5s6 2 6 5"/><path d="M16 6.2a3 3 0 0 1 0 5.6"/><path d="M18 18.6c0-2.2-1-4-2.8-4.8"/>',
  mega: '<path d="M4 10v4a1 1 0 0 0 1 1h3l5 4V5L8 9H5a1 1 0 0 0-1 1Z"/><path d="M17 9a4 4 0 0 1 0 6"/><path d="M19.5 6.5a8 8 0 0 1 0 11"/>'
};
const RK = O.risk_summary || {};
const observedLowRate = scannedCount > 0 ? lowStarCount / scannedCount : null;
const metricRows = [
  { label: S.source === "collection_info" ? "扫描评论" : "分析输入评论", value: fmtCount(scannedCount), note: "本次采集的去重样本", tone: "var(--brand)", soft: "var(--brand-soft)", icon: "doc" },
  { label: "低星评论", value: fmtCount(lowStarCount), note: scannedCount ? lowStarCount + " / " + scannedCount + " = " + fmtRate(observedLowRate, 1) : "未提供扫描总数", tone: "var(--medium)", soft: "var(--medium-soft)", icon: "users" },
  { label: "可分析低星正文", value: fmtCount(effectiveCount), note: "可供文本分析 · 占低星 " + fmtRate(lowStarCount ? effectiveCount / lowStarCount : null, 1), tone: "var(--violet)", soft: "var(--violet-soft)", icon: "doc" },
  { label: "购物关注方面", value: fmtCount(shoppingAspectCount), note: repeatedAspectCount + " 个方面有多条反馈", tone: "var(--high)", soft: "var(--high-soft)", icon: "alert" }
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

/* ---------- 购物关注概览与方面数量 ---------- */
function renderShoppingOverview() {
  const host = $("shoppingOverview");
  host.replaceChildren();
  const label = shoppingSummary.attention_label || (shoppingAspectCount ? "已观察到负面反馈，建议查看依据" : "当前样本不足以判断购物体验");
  host.append(el("div", "attention-title", label), el("p", "attention-description", "有负面评论的方面全部展示，不以商品页是否宣传为前提。"));
  const counts = el("div", "shopping-counts");
  [[shoppingSummary.negative_review_count == null ? coveredIds.size : shoppingSummary.negative_review_count, "已归类负面评论"],
   [shoppingAspectCount, "涉及方面"], [repeatedAspectCount, "多条反馈方面"]].forEach(([value, title]) => {
    const card = el("div", "shopping-count"); card.append(el("b", "", value), el("span", "", title)); counts.append(card);
  });
  host.append(counts);
  const safety = shoppingSummary.safety_aspects || [];
  if (safety.length) host.append(el("p", "safety-note", "安全相关线索：" + safety.join("、") + "。请先核查原文与使用条件。"));
  if (unclassifiedCount) {
    const button = el("button", "inline-button", "另有 " + unclassifiedCount + " 条低星正文未归入负面方面 · 查看原文");
    button.style.marginTop = "11px";
    button.addEventListener("click", () => { $("aspect").value = "__unclassified"; $("risk").value = ""; renderBars(); renderCards(); });
    host.append(button);
  }
  const promo = el("details", "promo-overview");
  promo.append(el("summary", "", "宣传矛盾对照（独立指标）"));
  promo.append(el("p", "", "明确矛盾评论 " + fmtCount(O.gap_evidence_count) + " 条，按评论去重。"));
  promo.append(el("p", "", RK.risk_index == null ? "暂无可评分的宣传矛盾证据。" : "最高宣传矛盾指数 " + Number(RK.risk_index).toFixed(1) + " / 100 · " + (RK.top_aspect || "") + "。"));
  promo.append(el("p", "", "此指标只比较宣传承诺与评论，不评价商品整体好坏；指数低或为空，也不会抵消已观察到的购物问题。"));
  host.append(promo);
}

function barRow(aspect, index) {
  const shopping = shoppingOf(aspect), tone = shoppingTone(aspect);
  const row = el("button", "bar-row" + ($("aspect").value === aspect.aspect ? " active" : ""));
  row.type = "button";
  row.setAttribute("aria-label", "筛选购物问题方面：" + aspect.aspect);
  row.style.setProperty("--tone", "var(--" + tone + ")");
  const top = el("div", "bar-top");
  top.append(el("span", "bar-name", aspect.aspect), el("span", "bar-value", shopping.negative_count + " 条"));
  const track = el("div", "track"), fill = el("div", "fill");
  fill.style.width = "0%"; track.append(fill);
  const foot = el("div", "bar-foot");
  foot.append(el("span", "lvl", shopping.attention_label), el("span", "", "占可分析低星 " + fmtRate(shopping.effective_share, 1)));
  row.append(top, track, foot);
  row.title = "横条比较负面评论数量；" + shopping.negative_count + " 条 / 扫描 " + scannedCount + " 条，不是发生概率。";
  row.addEventListener("click", () => {
    $("aspect").value = $("aspect").value === aspect.aspect ? "" : aspect.aspect;
    renderBars(); renderCards();
  });
  const maximum = Math.max(1, ...shoppingAspects.map((item) => shoppingOf(item).negative_count));
  const target = shopping.negative_count / maximum * 100;
  requestAnimationFrame(() => { setTimeout(() => { fill.style.width = target + "%"; }, reduced ? 0 : 40 + index * 30); });
  return row;
}

function renderBars() {
  const host = $("bars"); host.replaceChildren();
  const aspects = D.aspects || [];
  const others = aspects.filter((aspect) => !shoppingOf(aspect).negative_count);
  const rows = sortRows(showZero ? aspects : shoppingAspects);
  $("barCount").textContent = shoppingAspectCount + " 个方面有负面反馈 · " + repeatedAspectCount + " 个方面有多条反馈";
  const toggle = $("toggleZero"); toggle.hidden = !others.length;
  toggle.textContent = showZero ? "收起无负面记录的方面" : "无负面记录的方面 " + others.length;
  toggle.onclick = () => { showZero = !showZero; renderBars(); renderCards(); };
  if (!rows.length) { host.append(emptyBlock("暂无已归类的购物问题", "请结合低星正文与未归类原文核查，不能据此判断商品没有问题。")); return; }
  rows.forEach((aspect, index) => host.append(barRow(aspect, index)));
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
  if (gap.confidence_basis) body.append(el("p", "basis", "判断依据：" + gap.confidence_basis));

  const quotes = gap.quotes || [];
  if (quotes.length) {
    body.append(el("div", "ev-head", "相关评论证据"));
    quotes.forEach((quote) => {
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
  } else {
    body.append(el("p", "basis", "没有可展示的评论原文，当前判断仅基于已匹配的问题簇。"));
  }
  wrap.append(summary, body);
  return wrap;
}

function evidenceCard(review) {
  const card = el("div", "evidence"), top = el("div", "ev-top");
  top.append(starNode(review.star));
  const bits = ["评论 " + review.review_id];
  if (review.date) bits.push(review.date);
  if (review.sku) bits.push("SKU " + review.sku);
  top.append(el("span", "ev-meta", bits.join(" · ")));
  card.append(top, el("p", "ev-text", review.content || review.evidence || ""));
  return card;
}

function problemBlock(problem, shopping) {
  const detail = el("details", "problem");
  const ids = Array.from(new Set(problem.review_ids || []));
  const summary = el("summary");
  summary.append(el("span", "", problem.name || "未命名问题"), el("small", "", ids.length + " 条独立评论"));
  const body = el("div", "problem-body");
  if (problem.description) body.append(el("p", "", problem.description));
  const evidence = new Map(shopping.evidence.map((row) => [String(row.review_id), row]));
  ids.forEach((id) => { const review = evidence.get(String(id)) || reviewIndex.get(String(id)); if (review) body.append(evidenceCard(review)); });
  if (!ids.length) body.append(el("p", "basis", "当前问题未附逐条评论引用，请结合本方面原文核查。"));
  detail.append(summary, body); return detail;
}

function promoComparison(aspect) {
  const detail = el("details", "promo-comparison"), summary = el("summary");
  const value = aspect.risk_index == null ? "暂无可评分证据" : "宣传矛盾指数 " + Number(aspect.risk_index).toFixed(1) + " / 100";
  summary.append(el("b", "", "宣传矛盾对照（独立指标）"), el("span", "", value));
  const body = el("div", "promo-body"), structure = aspect.evidence_structure || {};
  const stats = el("div", "mini-stats");
  [["明确矛盾", (structure.contradiction == null ? aspect.contradiction_count || 0 : structure.contradiction) + " 条"],
   ["具体关联", (structure.related_negative == null ? aspect.possible_count || 0 : structure.related_negative) + " 条"],
   ["支持证据", (structure.support || aspect.support_evidence_count || 0) + " 条"]].forEach(([label, count]) => {
     const mini = el("div", "mini"); mini.append(el("small", "", label), el("b", "", count)); stats.append(mini);
   });
  body.append(stats);
  let note;
  if (aspect.risk_index == null) {
    note = !aspect.claim_count || aspect.null_reason === "no_claim"
      ? "商品页没有可比宣传承诺，因此不计算宣传矛盾指数；上方购物问题仍然需要关注。"
      : "暂未形成可评分的宣传矛盾证据；未评估或未检出矛盾，不等于没有购物问题。";
  } else {
    const calculation = aspect.risk_calculation || {};
    note = "宣传矛盾指数只表示当前证据对宣传承诺的质疑强度，未做概率校准，也不是商品整体购物风险。";
    note += " 参数版本 " + (calculation.params_version || "risk-v2") + "；证据充分性：" + (aspect.evidence_sufficiency_label || "未提供") + "。";
    if (aspect.legacy_direct_rate != null) note += " 明确矛盾评论 / 扫描评论 = " + fmtRate(aspect.legacy_direct_rate) + "，仅作样本对照。";
  }
  body.append(el("p", "aspect-note", note));
  const related = (aspect.gap_ids || []).map((id) => gaps.get(id)).filter(Boolean)
    .sort((a, b) => conflictRank(b) - conflictRank(a) || (b.support_review_count || 0) - (a.support_review_count || 0));
  related.forEach((gap) => body.append(claimBlock(gap, false)));
  if (!related.length) body.append(el("p", "basis", "没有对应的宣传对照明细。"));
  detail.append(summary, body); return detail;
}

function aspectCard(aspect, index) {
  const shopping = shoppingOf(aspect), tone = shoppingTone(aspect);
  const detail = el("details", "aspect-card");
  detail.dataset.aspect = aspect.aspect;
  detail.style.setProperty("--tone", "var(--" + tone + ")");
  detail.style.setProperty("--tone-soft", "var(--" + tone + "-soft)");
  detail.style.setProperty("--tone-ink", "var(--" + tone + "-ink)");
  detail.style.animationDelay = Math.min(index * 45, 280) + "ms";
  detail.open = expanded && index === 0;
  const summary = el("summary"), head = el("span", "head-text"), support = el("span", "head-support");
  const problemNames = shopping.problems.map((problem) => problem.name).filter(Boolean);
  head.append(el("span", "name", aspect.aspect), el("small", "", problemNames.length ? problemNames.join(" / ") : "负面评论已归入本方面，展开查看原文"));
  support.append(el("b", "", shopping.negative_count + " 条负面评论"), el("small", "", "占可分析低星 " + fmtRate(shopping.effective_share, 1)));
  summary.append(el("span", "idx", index + 1), head, el("span", "tag " + tone, shopping.attention_label), support, el("span", "chev"));
  const body = el("div", "aspect-body"), stats = el("div", "mini-stats");
  [["负面独立评论", shopping.negative_count + " 条"],
   ["占扫描样本（" + scannedCount + " 条）", fmtRate(shopping.scanned_rate, 2)],
   ["占低星评论（" + lowStarCount + " 条）", fmtRate(shopping.low_star_share, 1)],
   ["占可分析低星（" + effectiveCount + " 条）", fmtRate(shopping.effective_share, 1)]].forEach(([label, value]) => {
     const mini = el("div", "mini"); mini.append(el("small", "", label), el("b", "", value)); stats.append(mini);
   });
  body.append(stats, el("p", "aspect-note", "这些比例只描述本次样本；同一评论可涉及多个方面，不能相加为总人数，也不能解释成实际购买概率。"));
  const problems = el("div", "problem-list");
  shopping.problems.forEach((problem) => problems.append(problemBlock(problem, shopping)));
  if (!shopping.problems.length) problems.append(el("p", "basis", shopping.negative_count ? "暂无问题分组，可直接查看下面的负面评论原文。" : "本方面暂未归入负面问题，不能据此推断商品没有问题。"));
  body.append(problems);
  const reviews = el("details", "review-list");
  reviews.append(el("summary", "", "查看负面评论原文（" + shopping.evidence.length + " / " + shopping.negative_count + " 条）"));
  shopping.evidence.forEach((review) => reviews.append(evidenceCard(review)));
  if (shopping.evidence.length < shopping.negative_count) reviews.append(el("p", "basis", "部分评论没有可展示原文，请在导出的差评数据中核查。"));
  body.append(reviews, promoComparison(aspect));
  detail.append(summary, body); return detail;
}

function unclassifiedCard() {
  const detail = el("details", "aspect-card unclassified-card"); detail.open = true;
  detail.dataset.aspect = "__unclassified";
  const summary = el("summary"), head = el("span", "head-text"), support = el("span", "head-support");
  head.append(el("span", "name", "未归入负面方面的低星正文"), el("small", "", "可能为正面正文、无具体问题或抽取遗漏，请结合原文核查"));
  support.append(el("b", "", unclassifiedCount + " 条"), el("small", "", "不参与负面计数"));
  summary.append(el("span", "idx", "?"), head, el("span", "tag insufficient", "待核查"), support, el("span", "chev"));
  const body = el("div", "aspect-body");
  body.append(el("p", "aspect-note", "这些是可分析的低星正文，但尚未归入负面方面；可能为正面正文、无具体问题或抽取遗漏。请核查原文，不自动计作负面或正面证据。"));
  unclassifiedRows.forEach((review) => body.append(evidenceCard(review)));
  if (unclassifiedRows.length < unclassifiedCount) body.append(el("p", "basis", "当前报告原文覆盖不完整，请核查差评数据。"));
  detail.append(summary, body); return detail;
}

function aspectVisible(aspect) {
  if (shoppingOf(aspect).negative_count > 0) return true;
  if ($("aspect").value === aspect.aspect) return true;
  return showZero || $("risk").value === "none";
}

function corpusOf(aspect) {
  const shopping = shoppingOf(aspect), parts = [aspect.aspect];
  shopping.problems.forEach((problem) => parts.push(problem.name || "", problem.description || ""));
  shopping.evidence.forEach((review) => parts.push(review.content || review.evidence || ""));
  (aspect.gap_ids || []).forEach((id) => {
    const gap = gaps.get(id); if (!gap) return;
    parts.push(gap.claim_text || "", gap.claim_original_text || "", gap.finding || "", gap.confidence_basis || "");
    (gap.quotes || []).forEach((quote) => parts.push(quote.evidence || ""));
  });
  return parts.join(" ").toLowerCase();
}
function matches(aspect) {
  const level = $("risk").value;
  if (level && shoppingOf(aspect).attention_level !== level) return false;
  if ($("aspect").value && aspect.aspect !== $("aspect").value) return false;
  const query = $("search").value.trim().toLowerCase();
  return !query || corpusOf(aspect).includes(query);
}
function sortRows(rows) {
  const mode = $("sort").value;
  return rows.slice().sort((a, b) => {
    const sa = shoppingOf(a), sb = shoppingOf(b);
    if (mode === "promo") return ((b.risk_index == null ? -1 : b.risk_index) - (a.risk_index == null ? -1 : a.risk_index)) || sb.negative_count - sa.negative_count;
    if (mode === "negative") return sb.negative_count - sa.negative_count || (ATTENTION_ORDER[sb.attention_level] - ATTENTION_ORDER[sa.attention_level]);
    return (ATTENTION_ORDER[sb.attention_level] - ATTENTION_ORDER[sa.attention_level]) || sb.negative_count - sa.negative_count;
  });
}
function showUnclassified() {
  if (!unclassifiedCount) return false;
  if ($("aspect").value && $("aspect").value !== "__unclassified") return false;
  if ($("risk").value && $("risk").value !== "unclassified") return false;
  const query = $("search").value.trim().toLowerCase();
  return !query || ("未归入负面方面的低星正文 " + unclassifiedRows.map((review) => review.content || "").join(" ")).toLowerCase().includes(query);
}
function renderCards() {
  const host = $("cards"); host.replaceChildren();
  const rows = sortRows((D.aspects || []).filter(aspectVisible).filter(matches));
  const pending = showUnclassified();
  $("resultCount").textContent = rows.length + " 个购物问题方面" + (pending ? " · " + unclassifiedCount + " 条低星正文未归入负面方面" : "") + "；展开查看具体问题与原文";
  if (!rows.length && !pending) { host.append(emptyBlock("没有匹配结果", "调整搜索词或筛选条件；没有匹配结果不代表没有购物问题。")); return; }
  rows.forEach((aspect, index) => host.append(aspectCard(aspect, index)));
  if (pending) host.append(unclassifiedCard());
}

/* ---------- 筛选与弹窗 ---------- */
const aspectSelect = $("aspect");
aspectSelect.append(new Option("全部方面", ""));
(D.aspects || []).forEach((aspect) => aspectSelect.append(new Option(aspect.aspect, aspect.aspect)));
if (unclassifiedCount) aspectSelect.append(new Option("未归类低星正文", "__unclassified"));
["aspect", "risk", "sort"].forEach((id) => $(id).addEventListener("change", () => {
  if (id === "aspect") renderBars();
  renderCards();
}));
$("search").addEventListener("input", renderCards);
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
  const box = el("div");
  box.append(el("h3", "", "先看购物问题，再独立比较宣传"));
  box.append(el("p", "", "购物问题来自已抽取并核对的负面评论，不要求商品页先作出宣传承诺。重复提到的做工、尺码、异味等问题都直接展示；宣传矛盾少，不能解释成商品整体低风险。"));
  const formula = el("div", "formula");
  formula.append(
    el("div", "", "扫描样本 N = " + scannedCount + "；低星 L = " + lowStarCount + "；可分析低星正文 E = " + effectiveCount + "。"),
    el("div", "", "低星占比 = L / N = " + lowStarCount + " / " + scannedCount + " = " + fmtRate(observedLowRate, 1)),
    el("div", "", "各方面占扫描样本 = 该方面负面去重评论数 / N"),
    el("div", "", "各方面占低星评论 = 该方面负面去重评论数 / L"),
    el("div", "", "各方面占可分析低星 = 该方面负面去重评论数 / E")
  );
  box.append(formula, el("p", "", "一条评论可以涉及多个方面，因此各方面数量不能相加为总人数。初评与追评按评论 ID 去重。比例只描述已观察样本，不是实际购买概率，也不是商品总体故障率。"));
  box.append(el("h3", "", "关注提示怎么读"));
  const policy = shoppingSummary.policy || {};
  box.append(el("p", "", "个别负面反馈：当前仅有一条相关评论；多条负面反馈：至少 " + (policy.repeated_min_reviews || 2) + " 条独立评论提到问题。"));
  box.append(el("p", "", "重点关注：至少 " + (policy.concentrated_min_reviews || 3) + " 条负面评论，且占本次可分析低星正文的 " + fmtRate(policy.concentrated_min_effective_share == null ? 0.2 : policy.concentrated_min_effective_share, 0) + " 或以上。安全相关线索另行提醒，应结合原文核查。"));
  box.append(el("p", "", "这些是本项目的样本提醒规则，未经总体概率校准；不自动断言问题普遍存在，也不因为反馈少而给出安全保证。"));
  if (unclassifiedCount) box.append(el("p", "", "另有 " + unclassifiedCount + " 条低星正文未归入负面方面，主列表单独保留原文；不自动计作负面或正面证据。"));
  const promo = el("details", "promo-overview");
  promo.append(el("summary", "", "查看宣传矛盾指数的算法与证据规则"));
  promo.append(el("p", "", "宣传矛盾指数（0–100）是独立对照指标，只回答“当前评论证据是否质疑商品宣传”，不回答商品整体是否值得买。没有对应承诺、指数为空或指数低，都不会删除购物问题。"));
  const riskFormula = el("div", "formula");
  riskFormula.append(
    el("div", "", "单条证据权重 = 事实具体性 × 严重程度 × 引用核对系数"),
    el("div", "", "宣传矛盾指数 = 100 × W^n / (K^n + W^n) × 支持抵扣；W 为证据权重和"),
    el("div", "", "明确矛盾直接计入；具体关联按较低权重计入；仅提到关键词不计分"),
    el("div", "", "支持性证据必须具体且条件匹配；笼统好评不抵扣"),
    el("div", "", "参数为实验性工程规则，未做概率校准。Wilson 样本区间也不校正自选择、漏采或模型错判")
  );
  promo.append(riskFormula, el("p", "", "证据充分性和严重程度独立展示；它们用于解释宣传对照的依据，不代替负面评论的数量与内容。"));
  box.append(promo, el("h3", "", "样本与覆盖"));
  if (S.platform_total) box.append(el("p", "", "平台显示总量 " + S.platform_total + " 条，仅作背景，不作为比例分母。"));
  if (S.stop_reason) box.append(el("p", "", "采集停止原因：" + S.stop_reason));
  if (S.coverage_note) box.append(el("p", "", S.coverage_note));
  (D.limitations || []).forEach((note) => box.append(el("p", "", note)));
  if ((D.warnings || []).length) { box.append(el("h3", "", "数据提示")); (D.warnings || []).forEach((note) => box.append(el("p", "", note))); }
  openDialog("购物问题、宣传对照与样本口径", box);
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
  if ((D.excluded_claims || []).length) {
    const excluded = el("details");
    excluded.append(el("summary", "", "未纳入商品宣传对照的内容（" + D.excluded_claims.length + " 条）"));
    const excludedList = el("ol");
    D.excluded_claims.forEach((claim) => {
      const item = el("li", "", claim.original_text || claim.text || "");
      item.append(el("small", "", (claim.reason || "非商品承诺") + (claim.source ? " · " + claim.source : "")));
      excludedList.append(item);
    });
    excluded.append(excludedList); box.append(excluded);
  }
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

function riskTone() {
  const rk = D.overview.risk_summary || {};
  const safety = D.overview.safety_alert_count || 0;
  const evidence = D.overview.gap_evidence_count || 0;
  const index = rk.risk_index;
  if (safety > 0 || (index != null && index >= 65)) return { tone: "high", label: "高" };
  if (index != null && index >= 35) return { tone: "medium", label: "中" };
  if (evidence > 0) return { tone: "low", label: "低" };
  return { tone: "low", label: "低（未发现直接反驳宣传的证据）" };
}

function renderOverallRisk() {
  const host = $("overallRisk");
  const o = D.overview || {};
  const rk = o.risk_summary || {};
  const tone = riskTone();
  host.hidden = false;
  host.replaceChildren();

  const headline = el("div", "risk-headline");
  const badge = el("span", "risk-badge risk-" + tone.tone);
  badge.append(el("span", "dot"), el("span", "", "整体宣传落差风险：" + tone.label));
  headline.append(badge);
  const parts = [];
  parts.push(rk.risk_index == null
    ? "暂无可评分的宣传矛盾证据"
    : "最高宣传矛盾指数 " + Number(rk.risk_index).toFixed(1) + " / 100（" + (rk.top_aspect || "—") + "）");
  parts.push("明确矛盾证据 " + (o.gap_evidence_count || 0) + " 条");
  if (o.scored_aspect_count) parts.push("覆盖 " + o.scored_aspect_count + " 个方面");
  if (o.safety_alert_count > 0) parts.push("安全相关 " + o.safety_alert_count + " 项");
  headline.append(el("span", "risk-desc", parts.join(" · ") + "。指数未做概率校准，只表示证据强度，颜色越红越值得关注。"));
  host.append(headline);

  const scored = (D.aspects || []).filter((a) => a.risk_index != null)
    .sort((a, b) => b.risk_index - a.risk_index);
  const bands = rk.bands || { high: 0, medium: 0, low: 0 };
  const total = scored.length || 0;
  const overview = el("div", "risk-overview");
  const big = el("div", "risk-big");
  const indexText = rk.risk_index == null ? "—" : Number(rk.risk_index).toFixed(1);
  const indexColor = rk.risk_band === "high" ? "#a03125" : rk.risk_band === "medium" ? "#8a5a10" : "#2e6b3f";
  const number = el("b", "", indexText);
  number.style.color = indexColor;
  big.append(number, el("small", "", rk.risk_index == null
    ? "暂无形成指数的方面"
    : "最高宣传矛盾指数 / 100 · " + (rk.top_aspect || "—") + " · " + (rk.risk_band_label || "")));
  overview.append(big);

  const dist = el("div", "risk-dist");
  const bar = el("div", "risk-dist-bar");
  [["high", "seg-high"], ["medium", "seg-medium"], ["low", "seg-low"]].forEach(([key, cls]) => {
    const count = bands[key] || 0;
    if (!count || !total) return;
    const seg = el("span", cls);
    seg.style.width = (count / total * 100).toFixed(1) + "%";
    seg.title = count + " 个方面";
    bar.append(seg);
  });
  const legend = el("div", "risk-legend");
  [["高", "high", "#d64533"], ["中", "medium", "#e5972f"], ["低", "low", "#4c9e57"]].forEach(([label, key, color]) => {
    const item = el("span", "");
    const dot = el("i"); dot.style.background = color;
    item.append(dot, el("span", "", label + " " + (bands[key] || 0)));
    legend.append(item);
  });
  dist.append(bar, legend);
  const chips = el("div", "risk-detail");
  scored.slice(0, 4).forEach((a) => {
    const chip = el("span", "risk-chip", a.aspect + " ");
    chip.append(el("b", "", Number(a.risk_index).toFixed(1)));
    chips.append(chip);
  });
  if (!scored.length) chips.append(el("span", "risk-desc", "当前样本没有形成可评分的宣传矛盾证据（低不等于没问题，见下方重点留意）。"));
  dist.append(chips);
  overview.append(dist);
  host.append(overview);

  const hidden = (D.product_issues || []).filter((row) => row.review_count >= 3).slice(0, 6);
  const block = el("div", "hidden-risks");
  block.append(el("div", "hidden-risks-title",
    "重点留意：宣传未提及、但差评集中出现（这些不在上面的落差指数里）"));
  if (!hidden.length) {
    block.append(el("div", "hidden-risk-empty", "当前没有单方面差评 ≥3 条且宣传未提及的情况。"));
  } else {
    hidden.forEach((row) => {
      const line = el("div", "hidden-risk-row");
      line.append(el("b", "", row.problem_name),
        el("span", "", row.aspect),
        el("span", "cnt", row.review_count + " 条差评"),
        el("span", "", row.severity_label || "未分级"),
        el("span", "", "宣传未提及"));
      block.append(line);
    });
  }
  host.append(block);
  const note = el("div", "risk-desc", "说明：这些方面商品页没有对应承诺，不进入宣传落差指数；集中出现（≥3 条）即值得重点留意。完整问题列表见下方「购物问题与证据」。");
  host.append(note);
}

renderShoppingOverview();
renderOverallRisk();
renderBars();
renderCards();

// 应用内预览用 iframe 嵌入：把实际内容高度告诉父窗口，父窗口按高度展开，
// 避免固定高度导致右栏被裁、页脚停在半空。
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
</html>'''
