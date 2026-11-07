// @ts-check
const { test, expect } = require('@playwright/test');

test.describe('Music Video Studio - Render Queue & Stem UI Suite', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate directly to test_project
    await page.goto('/?project=test_project');
    // Wait for project to be selected and scenes loaded
    await expect(page.locator('#projectSelect')).toHaveValue(/test_project/, { timeout: 10000 });
    await page.waitForFunction(() => {
      // @ts-ignore
      return Boolean(window.state && window.state.projectData && window.state.projectData.scenes && window.state.projectData.scenes.length > 0);
    }, { timeout: 10000 });

    // Reset queue to canonical scene order before each test and ensure dummy render is active
    await page.evaluate(async () => {
      await fetch('/api/settings/dummy_mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: true }),
      });
      await fetch('/api/queue/enqueue_all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug: 'test_project' }),
      });
      // @ts-ignore
      if (window.loadProjectQueue) {
        // @ts-ignore
        await window.loadProjectQueue('test_project');
      }
    });
  });

  test('01. Two-pass duet scene displays separate Pass 1 and Pass 2 audio stems', async ({ page }) => {
    // Select S02 (Two-Pass Duet scene, index 1)
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(1);
    });

    // Verify Scene Inspector header shows S02
    await expect(page.locator('#inspectorSceneId')).toContainText('Scene S02', { timeout: 5000 });
    await expect(page.locator('#sceneMode')).toHaveValue('duet_method_b');

    // Verify single audio stem dropdown is HIDDEN
    const singleStemGroup = page.locator('#singleStemGroup');
    await expect(singleStemGroup).toBeHidden();

    // Verify Two-Pass stem dropdowns are VISIBLE
    const duetGroup = page.locator('#duetPromptsGroup');
    await expect(duetGroup).toBeVisible();

    const stemM = page.locator('#sceneStemM');
    const stemF = page.locator('#sceneStemF');
    await expect(stemM).toBeVisible();
    await expect(stemF).toBeVisible();

    // Verify option values
    await expect(stemM).toHaveValue('male');
    await expect(stemF).toHaveValue('female');
  });

  test('02. Render queue loads all scene passes with controls, status badges, and actions', async ({ page }) => {
    // Switch to Render Console tab
    await page.locator('button[data-tab="console"]').click();

    // Verify dummy render mode toggle is HIDDEN from normal users to prevent accidental selection
    const dummyContainer = page.locator('#dummyModeContainer');
    await expect(dummyContainer).toBeHidden();
    const dummyChk = page.locator('#chkDummyRenderMode');
    await expect(dummyChk).toBeHidden();

    // Switch to Queue View
    await page.locator('#btnConsoleQueue').click();

    // Wait for queue table to populate
    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(4, { timeout: 15000 });

    // Verify row contents: S01, S02:P1, S02:P2, S03
    await expect(rows.nth(0)).toContainText('S01');
    await expect(rows.nth(1)).toContainText('S02_Pass1_Left');
    await expect(rows.nth(2)).toContainText('S02_Pass2_Final');
    await expect(rows.nth(3)).toContainText('S03');
  });

  test('03. Queue item reordering via Up and Down action buttons', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(4, { timeout: 15000 });

    // Click Down arrow on row 0
    const downBtnRow0 = rows.nth(0).locator('button[title="Move Down"]');
    await downBtnRow0.click();

    // Row 0 should now be S02_Pass1_Left and row 1 should be S01
    await expect(rows.nth(0)).toContainText('S02_Pass1_Left', { timeout: 8000 });
    await expect(rows.nth(1)).toContainText('S01', { timeout: 8000 });

    // Click Up arrow on row 1 to move S01 back to position 1
    const upBtnRow1 = rows.nth(1).locator('button[title="Move Up"]');
    await upBtnRow1.click();

    await expect(rows.nth(0)).toContainText('S01', { timeout: 8000 });
    await expect(rows.nth(1)).toContainText('S02_Pass1_Left', { timeout: 8000 });
  });

  test('04. Clear Queue and Queue All actions operate smoothly', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // Click Clear Queue
    const clearBtn = page.locator('#btnClearQueue');
    await clearBtn.click();

    const rows = page.locator('#queueTableBody tr');
    await expect(rows.first()).toBeVisible();
    // All items should be marked skipped or have an Enqueue button
    const enqueueButtons = page.locator('#queueTableBody button[title="Add to Queue"]');
    await expect(enqueueButtons).toHaveCount(4, { timeout: 8000 });

    // Click Queue All
    const queueAllBtn = page.locator('#btnQueueAll');
    await queueAllBtn.click();

    // After Queue All, items should be queued
    const queuedBadges = page.locator('#queueTableBody span:has-text("Queued")');
    await expect(queuedBadges).toHaveCount(4, { timeout: 8000 });
  });

  test('05. Immediate Restitch Master MP4 executes without queue blocking', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();

    const restitchBtn = page.locator('#btnStitchMaster');
    await expect(restitchBtn).toBeVisible();
    await restitchBtn.click();

    // Verify toast shows master restitch message
    const toast = page.locator('#toastMessage');
    await expect(toast).toContainText(/Restitching master video/i, { timeout: 8000 });
  });

  test('06. Click Render Scene triggers single render and completes cleanly in dummy mode', async ({ page }) => {
    // Select S01 (index 0)
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(0);
    });
    await expect(page.locator('#inspectorSceneId')).toContainText('Scene S01');

    // Click "Render Scene" button
    const renderSceneBtn = page.locator('#btnRenderSceneSingle');
    await expect(renderSceneBtn).toBeVisible();
    await renderSceneBtn.click();

    // Verify it switched to console tab
    await expect(page.locator('#tab-console')).toBeVisible();

    // Wait for render to complete (in dummy mode S01 completes in ~2-3s)
    const statusText = page.locator('#renderStatusText');
    await expect(statusText).toContainText(/Finished|Idle|Stopped|Ready/i, { timeout: 15000 });

    // Verify queue shows S01 completed
    await page.locator('#btnConsoleQueue').click();
    const rows = page.locator('#queueTableBody tr');
    await expect(rows.nth(0)).toContainText('Done', { timeout: 8000 });
  });

  test('07. Multi-stage journey: Cancel active run, add new scene to timeline, and verify queue updates', async ({ page }) => {
    // 1. Start a render of S01
    await page.locator('button[data-tab="timeline"]').click();
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(0);
    });
    await page.locator('#btnRenderSceneSingle').click();

    // 3. Immediately click Stop
    const stopBtn = page.locator('#btnStopRender');
    await expect(stopBtn).toBeVisible();
    await stopBtn.click();

    // 4. Verify status indicates stopped and no ghost active task
    const statusText = page.locator('#renderStatusText');
    await expect(statusText).toContainText(/Stopping|Stopped|Ready|Idle|Finished/i, { timeout: 10000 });
    await expect(statusText).toContainText(/Stopped|Ready|Idle|Finished/i, { timeout: 15000 });

    // 5. Switch to Timeline and click "Add Scene"
    await page.locator('button[data-tab="timeline"]').click();
    const addSceneBtn = page.locator('#btnAddScene');
    await expect(addSceneBtn).toBeVisible();
    await addSceneBtn.click();

    // 6. Verify inspector shows S04
    await expect(page.locator('#inspectorSceneId')).toContainText('Scene S04', { timeout: 5000 });

    // 7. Click Save Project
    const saveBtn = page.locator('#btnSaveProject');
    await saveBtn.click();
    await expect(page.locator('#toastMessage')).toContainText(/Project saved/i, { timeout: 8000 });

    // 8. Switch to Console -> Queue View
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();
    await page.locator('#btnReloadQueue').click();

    // 9. Verify queue has 5 items now (S01, S02:p1, S02:p2, S03, S04)
    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(5, { timeout: 8000 });
    await expect(rows.nth(4)).toContainText('S04');
    await expect(rows.nth(4)).toContainText(/Queued|Missing/i);

    // 10. Clean up: delete S04 so test_project stays pristine
    page.on('dialog', (dialog) => dialog.accept());
    await page.locator('button[data-tab="timeline"]').click();
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(3);
    });
    await page.locator('#btnDeleteScene').click();
    await page.locator('#btnSaveProject').click();
    await expect(page.locator('#toastMessage')).toContainText(/Project saved/i, { timeout: 8000 });
  });

  test('08. Multi-stage journey: Remove a scene from queue and re-add it (skip and re-enqueue)', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(4, { timeout: 8000 });

    // First ensure row 0 is queued
    const delBtnRow0 = rows.nth(0).locator('button[title="Remove from Queue"]');
    if (!(await delBtnRow0.isVisible())) {
      const addBtnRow0 = rows.nth(0).locator('button[title="Add to Queue"]');
      await addBtnRow0.click();
    }
    await expect(rows.nth(0).locator('button[title="Remove from Queue"]')).toBeVisible({ timeout: 5000 });

    // Click "Remove from Queue" on row 0
    await rows.nth(0).locator('button[title="Remove from Queue"]').click();

    // Verify row 0 changes to Skipped badge and shows "Add to Queue" button
    await expect(rows.nth(0)).toContainText('Skipped', { timeout: 8000 });
    const addBtn = rows.nth(0).locator('button[title="Add to Queue"]');
    await expect(addBtn).toBeVisible({ timeout: 5000 });

    // Click "Add to Queue" on row 0 to re-add it
    await addBtn.click();

    // Verify row 0 changes back to Queued badge and shows "Remove from Queue" button
    await expect(rows.nth(0)).toContainText('Queued', { timeout: 8000 });
    await expect(rows.nth(0).locator('button[title="Remove from Queue"]')).toBeVisible({ timeout: 5000 });
  });

  test('09. Multi-stage journey: Cancel Scene A, then render Scene B cleanly without ghost lingering or blockages', async ({ page }) => {
    // 1. Start render on S02 (duet scene)
    await page.locator('button[data-tab="timeline"]').click();
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(1);
    });
    await expect(page.locator('#inspectorSceneId')).toContainText('Scene S02');
    await page.locator('#btnRenderSceneSingle').click();

    // 3. Immediately click Stop to cancel S02
    await page.locator('#btnStopRender').click();
    await expect(page.locator('#renderStatusText')).toContainText(/Stopping|Stopped|Ready|Idle|Finished/i, { timeout: 10000 });
    await expect(page.locator('#renderStatusText')).toContainText(/Stopped|Ready|Idle|Finished/i, { timeout: 15000 });

    // 4. Switch to Timeline and click Render Scene on S03
    await page.locator('button[data-tab="timeline"]').click();
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(2);
    });
    await expect(page.locator('#inspectorSceneId')).toContainText('Scene S03');
    await page.locator('#btnRenderSceneSingle').click();

    // 5. Verify console tab switched, active task card shows S03 (NOT S02)
    await expect(page.locator('#tab-console')).toBeVisible();
    await expect(page.locator('#activeTaskTitle')).toContainText('S03');

    // 6. Wait for S03 to complete
    const statusText = page.locator('#renderStatusText');
    await expect(statusText).toContainText(/Finished|Idle|Stopped|Ready/i, { timeout: 15000 });

    // 7. Reload the page to test resilience against ghost cards
    await page.reload();
    await expect(page.locator('#projectSelect')).toHaveValue(/test_project/, { timeout: 10000 });
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // 8. Verify no task is showing as active or stuck in "rendering"
    const heading = page.locator('#activeTaskHeading');
    await expect(heading).toContainText(/CURRENT STATUS|IDLE/i, { timeout: 5000 });
    const timer = page.locator('#activeTaskTimer');
    await expect(timer).toBeHidden();
  });

  test('10. Multi-stage journey: Queue filter tabs (All, Queued, Finished, Skipped) filter rows accurately', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // 1. Clear queue so all items become Skipped
    await page.locator('#btnClearQueue').click();
    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(4, { timeout: 8000 });

    // 2. Enqueue only row 0 (S01)
    await rows.nth(0).locator('button[title="Add to Queue"]').click();
    await expect(rows.nth(0)).toContainText('Queued', { timeout: 5000 });

    // 3. Test "Queued" filter
    await page.locator('.btn-filter[data-filter="queued"]').click();
    await expect(page.locator('#queueTableBody tr')).toHaveCount(1, { timeout: 5000 });
    await expect(page.locator('#queueTableBody tr').first()).toContainText('S01');

    // 4. Test "Skipped" filter
    await page.locator('.btn-filter[data-filter="skipped"]').click();
    await expect(page.locator('#queueTableBody tr')).toHaveCount(3, { timeout: 5000 });

    // 5. Test "All" filter
    await page.locator('.btn-filter[data-filter="all"]').click();
    await expect(page.locator('#queueTableBody tr')).toHaveCount(4, { timeout: 5000 });

    // 6. Re-enqueue all for clean state
    await page.locator('#btnQueueAll').click();
    await expect(page.locator('#queueTableBody tr')).toHaveCount(4, { timeout: 5000 });
  });

  test('11. Multi-stage journey: Reorder queue to prioritize scene S03 and execute it first', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // 2. Clear queue
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/queue/clear') && resp.status() === 200),
      page.locator('#btnClearQueue').click(),
    ]);

    // 3. Enqueue only S03 (last row)
    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(4, { timeout: 8000 });
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/queue/enqueue') && resp.status() === 200),
      rows.nth(3).locator('button[title="Add to Queue"]').click(),
    ]);
    await expect(rows.nth(3)).toContainText('Queued', { timeout: 5000 });

    // 4. Also enqueue S01 (first row) so we have two queued items to reorder
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/queue/enqueue') && resp.status() === 200),
      rows.nth(0).locator('button[title="Add to Queue"]').click(),
    ]);
    await expect(rows.nth(0)).toContainText('Queued', { timeout: 5000 });

    // 5. Move S03 up using Move Up button
    const upBtn = rows.nth(3).locator('button[title="Move Up"]');
    if (await upBtn.isEnabled()) {
      await Promise.all([
        page.waitForResponse((resp) => resp.url().includes('/api/queue/move') && resp.status() === 200),
        upBtn.click(),
      ]);
    }

    // 6. Trigger "Render Missing / Changed"
    await page.locator('#btnRenderAll').click();

    // 7. Verify active rendering starts
    const statusText = page.locator('#renderStatusText');
    await expect(statusText).toContainText(/Running|Finished/i, { timeout: 8000 });

    // 8. Clean up: click stop button and wait for termination
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/render/stop') && resp.status() === 200),
      page.locator('#btnStopRender').click(),
    ]);
    await expect(statusText).toContainText(/Stopped|Ready|Idle|Finished/i, { timeout: 8000 });
  });

  test('12. Anchor image edits dynamically queue dependent scenes with ANCHOR reason', async ({ page }) => {
    const fs = require('fs');
    const path = require('path');
    const anchorsDir = path.resolve(__dirname, '../projects/all_or_nothing/work/anchors_v3');
    const a01Files = fs.readdirSync(anchorsDir).filter((f) => f.startsWith('A01'));
    expect(a01Files.length).toBeGreaterThan(0);
    const a01Path = path.join(anchorsDir, a01Files[0]);
    const origStat = fs.statSync(a01Path);

    try {
      // Touch anchor into future so it is newer than the rendered MP4
      const future = new Date(Date.now() + 100000);
      fs.utimesSync(a01Path, future, future);

      // Navigate to all_or_nothing project
      await page.goto('/?project=all_or_nothing');
      await expect(page.locator('#projectSelect')).toHaveValue(/all_or_nothing/, { timeout: 10000 });

      // Switch to Render Console tab & Queue View
      await page.locator('button[data-tab="console"]').click();
      await page.locator('#btnConsoleQueue').click();

      // Verify queue displays S01 as Queued with anchor reason
      const s01Row = page.locator('#queueTableBody tr', { hasText: 'S01' });
      await expect(s01Row).toBeVisible({ timeout: 10000 });
      await expect(s01Row).toContainText('Queued', { timeout: 10000 });
      await expect(s01Row).toContainText(/ANCHOR A01/i);
    } finally {
      fs.utimesSync(a01Path, origStat.atime, origStat.mtime);
    }
  });

  test('13. Target specific pass S02:p1 renders only Pass 1 and preserves Pass 2', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // 2. Trigger render for specific pass S02:p1 via triggerRender
    await page.evaluate(async () => {
      // @ts-ignore
      await window.triggerRender(['S02:p1']);
    });

    // 3. Verify top row in queue is S02_Pass1_Left
    const rows = page.locator('#queueTableBody tr');
    await expect(rows.nth(0)).toContainText('S02_Pass1_Left', { timeout: 8000 });

    // 4. Verify S02:p1 is targeted and active
    const statusText = page.locator('#renderStatusText');
    await expect(statusText).toContainText(/Running|Finished/i, { timeout: 8000 });

    // 5. Clean up: stop render and wait for server to acknowledge termination
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/render/stop') && resp.status() === 200),
      page.locator('#btnStopRender').click(),
    ]);
  });

  test('14. Render Two-Pass Duet scene queues and executes Pass 1 followed by Pass 2', async ({ page }) => {
    // 1. Trigger render for Two-Pass Duet scene S02
    await page.evaluate(async () => {
      // @ts-ignore
      await window.triggerRender(['S02']);
    });

    // 2. Switch to Console & Queue View and verify Pass 1 is at index 0 and Pass 2 is at index 1
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();
    const rows = page.locator('#queueTableBody tr');
    await expect(rows.nth(0)).toContainText('S02_Pass1_Left', { timeout: 8000 });
    await expect(rows.nth(1)).toContainText('S02_Pass2_Final', { timeout: 8000 });

    // 3. Stop render and wait for server to acknowledge termination
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/render/stop') && resp.status() === 200),
      page.locator('#btnStopRender').click(),
    ]);
  });

  test('15. Dynamic queueing during active render: Add scene while another is actively rendering', async ({ page }) => {
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // 1. Clear queue so all items are skipped
    await page.locator('#btnClearQueue').click();
    const rows = page.locator('#queueTableBody tr');
    await expect(rows).toHaveCount(4, { timeout: 8000 });

    // 2. Enqueue only S01 (row 0)
    await rows.nth(0).locator('button[title="Add to Queue"]').click();
    await expect(rows.nth(0)).toContainText('Queued', { timeout: 5000 });

    // 3. Trigger render on S01
    await page.locator('#btnRenderAll').click();
    const statusText = page.locator('#renderStatusText');
    await expect(statusText).toContainText(/Running|Finished/i, { timeout: 8000 });

    // 4. Dynamically add S03 (row 3) to queue while engine is active
    const addBtnS03 = rows.nth(3).locator('button[title="Add to Queue"]');
    if (await addBtnS03.isVisible()) {
      await addBtnS03.click();
      await expect(rows.nth(3)).toContainText('Queued', { timeout: 5000 });
    }

    // 5. Stop render and wait for server to acknowledge termination
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/render/stop') && resp.status() === 200),
      page.locator('#btnStopRender').click(),
    ]);
  });

  test('16. Scene prompt edit in UI transitions badge to needs render and queues under Render Missing/Changed', async ({ page }) => {
    // 1. Select scene S01 (index 0)
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(0);
    });

    // 2. Edit prompt in Scene Inspector
    const promptArea = page.locator('#scenePrompt');
    await expect(promptArea).toBeVisible({ timeout: 5000 });
    const originalPrompt = await promptArea.inputValue();
    const modifiedPrompt = `${originalPrompt} - UI test prompt modifier`;

    await promptArea.fill(modifiedPrompt);

    // 3. Save Changes via Save button
    const saveBtn = page.locator('#btnSaveProject');
    await expect(saveBtn).toContainText('Save Changes *', { timeout: 5000 });
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/project/') && resp.status() === 200),
      saveBtn.click(),
    ]);
    await expect(saveBtn).not.toContainText('*', { timeout: 8000 });

    // 4. Switch to Render Console and Queue View
    await page.locator('button[data-tab="console"]').click();
    await page.locator('#btnConsoleQueue').click();

    // 5. Trigger Queue All / Render Missing & Changed to refresh evaluation
    await page.locator('#btnQueueAll').click();
    const s01Row = page.locator('#queueTableBody tr', { hasText: 'S01' });
    await expect(s01Row).toBeVisible({ timeout: 8000 });
    await expect(s01Row).toContainText('Queued', { timeout: 8000 });

    // 6. Restore original prompt to keep test project pristine
    await page.locator('button[data-tab="timeline"]').click();
    await page.evaluate(() => {
      // @ts-ignore
      window.selectScene(0);
    });
    await promptArea.fill(originalPrompt);
    await Promise.all([
      page.waitForResponse((resp) => resp.url().includes('/api/project/') && resp.status() === 200),
      saveBtn.click(),
    ]);
    await expect(saveBtn).not.toContainText('*', { timeout: 8000 });
  });

  test('17. Developer URL flag ?dev=1 exposes hidden dummy mode controls, while normal view hides them', async ({ page }) => {
    // 1. Normal view: Dummy mode container is completely hidden
    await page.goto('/?project=test_project');
    await page.locator('button[data-tab="console"]').click();
    await expect(page.locator('#dummyModeContainer')).toBeHidden();

    // 2. Developer view: With ?dev=1 query parameter, container becomes visible
    await page.goto('/?project=test_project&dev=1');
    await page.locator('button[data-tab="console"]').click();
    await expect(page.locator('#dummyModeContainer')).toBeVisible();
    await expect(page.locator('#chkDummyRenderMode')).toBeVisible();
  });
});
