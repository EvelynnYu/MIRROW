import {test, expect} from '@playwright/test';
for (const width of [1280, 390]) test(`local shell ${width}px`, async ({page}, info) => {
  await page.setViewportSize({width, height: 900});
  const state = {configured: true, profile: {human_name: '测试人', agent_name: '测试伙伴', persona: '合成设定', relationship_started_at: ''},
    lifecycle: {preferences:{covenant_word:'',window_start:'22:00',window_end:'03:00',wait_minutes:30,day_change_hour:6,automatic_digest:true,autonomous_wander:false,idle_minutes:15,sentinel_enabled:false},
      user_status:{status:'unknown',detail:''},active_day:{date:'2026-10-13'},reservation:null,days:[],error:''},
    messages: [] as any[], wander: {running: false, stopping: false, error: ''}};
  await page.route('**/api/local/state', route => route.fulfill({json: state}));
  await page.route('**/api/local/presence',route => route.fulfill({json:{saved:true}}));
  await page.route('**/api/local/lifecycle/settings',route => {state.lifecycle.preferences = route.request().postDataJSON(); return route.fulfill({json:state.lifecycle});});
  await page.route('**/api/wander/persona', route => route.fulfill({json: {ai_name: '测试伙伴', user_name: '测试人'}}));
  await page.route('**/api/local/profile', route => {state.profile = route.request().postDataJSON(); return route.fulfill({json: {saved: true}});});
  await page.route('**/api/local/chat', route => {
    const body = route.request().postDataJSON();
    state.messages.push({message_id: body.request_id, role:'user',content:body.content,timestamp:new Date().toISOString()},
      {message_id:'reply_' + body.request_id,role:'assistant',content:'这是合成回复，用于验证真实请求确认后才出现消息。',timestamp:new Date().toISOString()});
    return route.fulfill({json:{state:'complete'}});
  });
  await page.route('**/api/local/memory?*', route => route.fulfill({json: {hits: []}}));
  await page.goto('/');
  await page.getByLabel('消息', {exact: true}).fill('合成问候');
  await page.getByRole('button', {name:'发送', exact:true}).click();
  await expect(page.getByText('这是合成回复，用于验证真实请求确认后才出现消息。')).toBeVisible();
  await page.screenshot({path:info.outputPath(`local-${width}.png`)});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.getByRole('button', {name:'设定',exact:true}).click();
  await page.getByLabel('AI 的名字').fill('新的测试伙伴');
  await page.getByRole('button', {name:'保存设定'}).click();
  await expect(page.getByText('已保存到本地。')).toBeVisible();
  await page.getByRole('button', {name:'记忆',exact:true}).click();
  await page.getByLabel('搜索',{exact:true}).fill('没有记录');
  await page.getByRole('button',{name:'查找记忆'}).click();
  await expect(page.getByText('没有命中已编码记忆。原始聊天仍保留在本地。')).toBeVisible();
  await page.getByRole('button',{name:'日常',exact:true}).click();
  await page.getByLabel('约定词（留空关闭关键词触发）').fill('晚安');
  await page.getByRole('button',{name:'保存日常设置'}).click();
  await expect(page.getByText('已更新',{exact:true})).toBeVisible();
  expect(state.lifecycle.preferences.covenant_word).toBe('晚安');
  await page.screenshot({path:info.outputPath(`daily-${width}.png`)});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
