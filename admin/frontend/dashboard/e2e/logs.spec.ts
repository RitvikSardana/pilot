import { expect, test } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { rm, writeFile } from 'node:fs/promises'
import { isAbsolute, resolve } from 'node:path'

const initialLogName = `e2e-browser-initial-${randomUUID()}.log`
const selectedLogName = `e2e-browser-selected-${randomUUID()}.log`
const initialLogLine = 'Playwright initial log marker'
const selectedLogLine = 'Playwright selected log marker'
const createdLogPaths: string[] = []

test.describe.configure({ mode: 'serial' })

test.beforeAll(async () => {
  const benchRoot = process.env.E2E_BENCH_ROOT
  if (!benchRoot || !isAbsolute(benchRoot)) {
    throw new Error('E2E_BENCH_ROOT must be the absolute path to the bench under test')
  }

  const initialLogPath = resolve(benchRoot, 'logs', initialLogName)
  const selectedLogPath = resolve(benchRoot, 'logs', selectedLogName)

  await writeFile(initialLogPath, `${initialLogLine}\n`, { flag: 'wx' })
  createdLogPaths.push(initialLogPath)
  await writeFile(selectedLogPath, `${selectedLogLine}\n`, { flag: 'wx' })
  createdLogPaths.push(selectedLogPath)
})

test.afterAll(async () => {
  for (const path of createdLogPaths) await rm(path)
})

test('Opens a log file', async ({ page }) => {
  await page.goto(`/insights/logs?file=${initialLogName}`)
  await expect(page.getByText(initialLogLine)).toBeVisible()
  await page.locator('aside').getByRole('button').filter({ hasText: selectedLogName }).click()

  await expect(page).toHaveURL((url) => url.searchParams.get('file') === selectedLogName)
  await expect(page.getByText(selectedLogLine)).toBeVisible()
})

test('Searches log files', async ({ page }) => {
  await page.goto('/insights/logs')
  await page.getByPlaceholder('Search log files').fill(initialLogName)

  await expect(page.getByRole('button').filter({ hasText: initialLogName })).toBeVisible()
  await expect(page.getByRole('button').filter({ hasText: selectedLogName })).toBeHidden()

  await page.getByPlaceholder('Search log files').fill(selectedLogName)
  await expect(page.getByRole('button').filter({ hasText: selectedLogName })).toBeVisible()
  await expect(page.getByRole('button').filter({ hasText: initialLogName })).toBeHidden()
})
