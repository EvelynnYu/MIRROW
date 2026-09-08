import { test, expect } from '@playwright/test';

for (const viewport of [{width: 1280, height: 900}, {width: 390, height: 844}]) {
  test(`logs and wish roundtrip at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.route('**/api/wander/persona', route => route.fulfill({json:{ai_name:'测试AI',user_name:'测试用户'}}));
    const now = new Date().toISOString();
    const wish: any = {id: 1, feature: '测试愿望：记住一次散步', reason: '合成测试材料，不是真实记忆。', status: 'open', times_wished: 2, updated_at: now, all_comments: []};
    await page.route('**/api/wander/logs?*', route => route.fulfill({json: {logs: [
      {event_id:'synthetic-activity', event_type:'browse_xiaohongshu', description:'测试活动：读到一篇植物笔记', state:'completed', created_at:now, ended_at:now, duration_seconds:30,
       details:{run_id:'synthetic-run', delivery_status:'suppressed'}, judgment_result:{share:true},
       nodes:[{round_index:1, status:'succeeded', source_summary:'这是一条合成的来源摘要。', reflection:'这是一段用于界面验证的节点感想。', duration_seconds:30}]},
    ], stats:{total_entries:1}}}));
    await page.route('**/api/wander/wishes**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'GET') return route.fulfill({json:{wishes:[wish]}});
      if (url.pathname.endsWith('/status')) wish.status = request.postDataJSON().status;
      else if (request.method() === 'POST') wish.all_comments.push({id:5,wish_id:1,author:'user',content:request.postDataJSON().content,created_at:now});
      else if (request.method() === 'PUT') wish.all_comments[0].content = request.postDataJSON().content;
      else if (request.method() === 'DELETE') wish.all_comments = [];
      return route.fulfill({json:{success:true,mutated:true}});
    });
    await page.goto('/');
    await expect(page.getByText('想分享 · 勿扰中未发送')).toBeVisible();
    await page.getByText('查看活动与节点').click();
    await expect(page.getByText('这是一段用于界面验证的节点感想。', {exact:false})).toBeVisible();
    await page.screenshot({path:testInfo.outputPath(`logs-${viewport.width}.png`)});
    await page.getByRole('button', {name:'打开许愿板'}).click();
    await expect(page.getByText('测试AI 的许愿板', {exact:false})).toBeVisible();
    await expect(page.getByRole('heading', {name:wish.feature})).toBeVisible();
    const input = page.locator('.wish-comment-compose input');
    const commentText = '合成评论：这是一段较长的测试文字，用来确认时间独立显示，评论正文可以自然换行，不会被日期挤成一窄条。';
    await input.fill(commentText);
    await page.getByRole('button', {name:'发送', exact:true}).click();
    await expect(page.getByText(commentText, {exact:true})).toBeVisible();
    const headerBox = await page.locator('.wish-comment-header').boundingBox();
    const contentBox = await page.locator('.wish-comment-content').boundingBox();
    expect(contentBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height);
    expect(contentBox!.width).toBeGreaterThan(viewport.width === 390 ? 260 : 160);
    await page.screenshot({path:testInfo.outputPath(`wishes-${viewport.width}.png`)});
    await page.getByRole('button', {name:'编辑', exact:true}).click();
    await page.locator('.wish-comment-edit-row input').fill('修改后的合成评论');
    await page.getByRole('button', {name:'保存', exact:true}).click();
    await expect(page.getByText('修改后的合成评论', {exact:true})).toBeVisible();
    page.on('dialog', dialog => dialog.accept());
    await page.getByRole('button', {name:'删除', exact:true}).click();
    await expect(page.getByText('修改后的合成评论', {exact:true})).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

test('offline is not an empty success and unmount cancels retries', async ({ page }) => {
  await page.route('**/api/wander/persona', route => route.fulfill({status:503,json:{detail:'unavailable'}}));
  let requests = 0;
  await page.route('**/api/wander/logs?*', route => { requests++; return route.fulfill({status:503,json:{detail:'unavailable'}}); });
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('未能刷新日志');
  await expect(page.getByText('所选日期暂无漫想记录')).toHaveCount(0);
  await page.getByRole('button', {name:'关闭漫想日志'}).click();
  const count = requests;
  await page.waitForTimeout(11000);
  expect(requests).toBe(count);
});
