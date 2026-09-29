// M05-AC-027 (D1-ext, P1): click picking through M06's Picker and M05's ID pass. The M05 side (PointPicker.prepare /
// decode, ID material, pick object on CH_PICK) is delivered and covered by tests/pointcloud/engine.browser.test.ts; the
// end-to-end click needs M06's pick pass (request .cache/impl/requests/M05-to-M06.md item 4), so the spec stays fixme.
import { test } from '@playwright/test'

test.fixme('point pick returns the clicked point within its node spacing (M05-AC-027)', async () => {})
