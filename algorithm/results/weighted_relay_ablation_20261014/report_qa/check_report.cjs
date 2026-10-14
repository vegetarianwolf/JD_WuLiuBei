const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('/Users/vegetarianwolf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = '/Users/vegetarianwolf/Projects/JD_WuLiuBei';
const out = path.join(root, 'algorithm/results/weighted_relay_ablation_20261014');
const shotDir = path.join(out, 'report_qa');
fs.mkdirSync(shotDir, { recursive: true });
const errors = [], external = [], snapshots = [], interactions = [];
const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const assert = (ok, message) => { if (!ok) throw new Error(message); };
async function geometry(page) {
  return page.evaluate(() => {
    const visible = e => e.getClientRects().length > 0;
    const clipped = [];
    for (const svg of document.querySelectorAll('svg.chart')) {
      if (!visible(svg)) continue;
      const r = svg.getBoundingClientRect();
      for (const text of svg.querySelectorAll('text')) {
        const b = text.getBoundingClientRect();
        if (b.left < r.left - 1.5 || b.right > r.right + 1.5 || b.top < r.top - 1.5 || b.bottom > r.bottom + 1.5)
          clipped.push({ svg: svg.id, label: text.textContent, bounds: [b.left-r.left,b.top-r.top,b.right-r.left,b.bottom-r.top], size: [r.width,r.height] });
      }
    }
    return { bodyOverflow: document.documentElement.scrollWidth - innerWidth, clipped,
      charts: [...document.querySelectorAll('svg.chart')].filter(visible).length,
      rawRows: document.querySelectorAll('#raw-table tbody tr').length,
      pairedRows: document.querySelectorAll('#paired-table tbody tr').length,
      taskRows: document.querySelectorAll('#task-effects-table tbody tr').length,
      scatterMarks: document.querySelectorAll('#task-effects circle, #task-effects path').length,
      factorText: document.querySelector('#factor-table').textContent,
      conclusion: document.querySelector('#conclusion').textContent,
      paperConclusion: document.querySelector('#paper-conclusion').textContent,
      mapLabels: [...document.querySelectorAll('#task-map text')].map(t=>t.textContent),
      concrete: document.querySelector('#concrete-case').textContent,
      primarySelection: document.querySelector('#mechanism-run').value,
      handoffDetail: document.querySelector('#handoff-detail').textContent,
      counterfactualSummary: document.querySelector('#counterfactual-summary').textContent,
      minimumSvgFont: Math.min(...[...document.querySelectorAll('svg.chart text')].filter(visible).map(t=>parseFloat(getComputedStyle(t).fontSize))) };
  });
}
(async () => {
 const browser = await chromium.launch({ headless: true });
 try {
  const context = await browser.newContext({ viewport: {width:1200,height:950}, offline: true, colorScheme: 'light' });
  const page = await context.newPage();
  page.on('pageerror', e=>errors.push(e.message));
  page.on('console', m=>{ if(m.type()==='error') errors.push(m.text()); });
  page.on('request', r=>{ if(/^https?:/.test(r.url())) external.push(r.url()); });
  await page.goto('file://' + path.join(out,'index.html'));
  await page.waitForFunction(()=>document.querySelectorAll('#raw-table tbody tr').length===12);
  const widths = [1200,360,320];
  for (const width of widths) {
   await page.setViewportSize({width,height:950});
   await page.waitForTimeout(150);
   const g = await geometry(page);
   snapshots.push({ width, colorScheme:'light', ...g });
   const runIds = await page.locator('#mechanism-run option').evaluateAll(es=>es.map(e=>e.value));
   assert(runIds.length===6,'Expected all six relay runs');
   for (const runId of runIds) {
    await page.selectOption('#mechanism-run',runId);
    const taskIds = await page.locator('#handoff-task option').evaluateAll(es=>es.map(e=>e.value));
    for (const taskId of taskIds) {
     await page.selectOption('#handoff-task', taskId);
     const actual = await geometry(page);
     interactions.push({width,runId,taskId,bodyOverflow:actual.bodyOverflow,clipped:actual.clipped,
       taskRows:actual.taskRows,scatterMarks:actual.scatterMarks,detail:actual.handoffDetail,summary:actual.counterfactualSummary});
    }
   }
  }
  await page.emulateMedia({colorScheme:'dark'});
  await page.setViewportSize({width:360,height:950});
  const darkRunIds = await page.locator('#mechanism-run option').evaluateAll(es=>es.map(e=>e.value));
  for (const runId of darkRunIds) {
   await page.selectOption('#mechanism-run',runId);
   const taskIds = await page.locator('#handoff-task option').evaluateAll(es=>es.map(e=>e.value));
   for (const taskId of taskIds) {
    await page.selectOption('#handoff-task',taskId);
    const actual = await geometry(page);
    interactions.push({width:360,colorScheme:'dark',runId,taskId,bodyOverflow:actual.bodyOverflow,clipped:actual.clipped,
      taskRows:actual.taskRows,scatterMarks:actual.scatterMarks,detail:actual.handoffDetail,summary:actual.counterfactualSummary});
   }
  }
  await page.emulateMedia({colorScheme:'light'});
  await page.setViewportSize({width:1200,height:950});
  await page.selectOption('#mechanism-run','direct_relay_stations__seed_2026101471');
  await page.selectOption('#handoff-task','132');
  await page.waitForTimeout(100);
  await page.evaluate(()=>scrollTo(0,0));
  await page.screenshot({path:path.join(shotDir,'desktop-overview.png')});
  await page.locator('section').filter({has:page.locator('#relay-heading')}).screenshot({path:path.join(shotDir,'desktop-cross-mechanism.png')});
  await page.setViewportSize({width:320,height:950});
  await page.waitForTimeout(100);
  await page.locator('#handoff-figures').screenshot({path:path.join(shotDir,'mobile-320-cross-mechanism.png')});
  await page.emulateMedia({colorScheme:'dark'});
  await page.setViewportSize({width:360,height:950});
  await page.selectOption('#mechanism-run','direct_relay__seed_2026101472');
  await page.selectOption('#handoff-task','52');
  await page.waitForTimeout(100);
  const dark = await geometry(page);
  snapshots.push({width:360,colorScheme:'dark',...dark});
  await page.locator('section').filter({has:page.locator('#mechanism-heading')}).screenshot({path:path.join(shotDir,'mobile-360-same-uav-dark.png')});
  const allGeometry = [...snapshots, ...interactions];
  const clipped = allGeometry.flatMap(g=>g.clipped.map(c=>({width:g.width,runId:g.runId,taskId:g.taskId,...c})));
  const audit = JSON.parse(fs.readFileSync(path.join(out,'audit.json'),'utf8'));
  const report = {
   passed: errors.length===0 && external.length===0 && clipped.length===0 && allGeometry.every(g=>g.bodyOverflow<=1&&g.taskRows===200&&g.scatterMarks===200),
   html_sha256: hash(path.join(out,'index.html')),
   renderer_sha256: hash(path.join(root,'algorithm/experiments/render_weighted_relay_ablation_report.py')),
   source_manifest_sha256: hash(path.join(out,'manifest.json')),
   source_runs_sha256: hash(path.join(out,'runs.json')),
   source_audit_sha256: hash(path.join(out,'audit.json')),
   paper_figures_validation_sha256: hash(path.join(out,'figures/validation.json')),
   environment: {engine:'Playwright Chromium',browser_version:browser.version(), mode:'direct file URL / offline context', browser_resources_closed:true,no_temporary_server:true},
   tested_widths:widths, color_schemes:['light','dark'], unique_relay_runs:6, unique_handoff_task_options:11,
   interaction_checks:interactions, overview_checks:snapshots, runtime_errors:errors, external_requests:external,
   clipped_labels:clipped,
   outcome_scope_checks: {origin_three_weighted_losses: snapshots[0].conclusion.includes('0胜/0平/3负'),
     predeployment_weighted_two_wins_one_loss:snapshots[0].conclusion.includes('2胜/0平/1负'),
     fractional_means:snapshots[0].factorText.includes('133.33')&&snapshots[0].factorText.includes('132.33')&&snapshots[0].factorText.includes('138.67'),
     concrete_cross_case:snapshots[0].concrete.includes('任务132')&&snapshots[0].concrete.includes('机2→机5')&&snapshots[0].concrete.includes('完全相同'),
     default_case_is_concrete:snapshots[0].primarySelection==='direct_relay_stations__seed_2026101471',
     same_uav_cache_boundary:dark.counterfactualSummary.includes('任务52')&&dark.counterfactualSummary.includes('机6→机6')&&dark.counterfactualSummary.includes('+7项')&&dark.counterfactualSummary.includes('不能证明跨机创新')},
   screenshots:Object.fromEntries(fs.readdirSync(shotDir).filter(n=>n.endsWith('.png')).map(n=>['report_qa/'+n,hash(path.join(shotDir,n))])),
   independently_audited_runs:audit.run_count,
   qa_script_path:'report_qa/check_report.cjs',qa_script_sha256:hash(__filename), no_optimizers_run:true,
  };
  report.passed = report.passed && Object.values(report.outcome_scope_checks).every(Boolean) && snapshots.every(g=>g.minimumSvgFont>=11&&g.rawRows===12&&g.pairedRows===6);
  fs.writeFileSync(path.join(out,'report_validation.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({passed:report.passed,errors,external,clipped:clipped.slice(0,30),clipped_count:clipped.length,scope:report.outcome_scope_checks,interactions:interactions.length}));
  await context.close();
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
