// 在 Chromium 中执行 Safari 分享预处理脚本，验证正文提取与属性列表安全性。
// 用法：node ios/Preprocess/test/run.js  （需要 Playwright；Safari 真机效果仍需在 S0 手动验证）
const path = require("path");
const fs = require("fs");
const assert = require("assert");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "../../..");
const bundle = fs.readFileSync(path.join(root, "ios/ShareExtension/Resources/GatherloomPreprocess.js"), "utf8");
const fixture = (name) => fs.readFileSync(path.join(__dirname, "fixtures", name), "utf8");

const cases = [
  {
    name: "普通博客（Readability）",
    url: "https://blog.example.com/swiftui-state",
    html: fixture("blog.html"),
    check(r) {
      assert.strictEqual(r.extractor, "readability-0.6.0");
      assert.ok(r.textContent.includes("Observation 框架"));
      assert.ok(r.textContent.includes("## 迁移步骤"), "保留二级标题");
      assert.ok(r.textContent.includes("- 更少的视图刷新"), "保留列表");
      assert.ok(r.textContent.includes("    var draft = \"\""), "保留代码缩进");
      assert.ok(!r.textContent.includes("侧边栏推荐"), "去掉侧边栏");
      assert.ok(!r.textContent.includes("页脚文字"), "去掉页脚");
      assert.strictEqual(r.canonicalURL, "https://blog.example.com/swiftui-state");
      assert.strictEqual(r.siteName, "某技术博客");
    },
  },
  {
    name: "微信公众号（站点规则）",
    url: "https://mp.weixin.qq.com/s/test-article",
    html: fixture("wechat.html"),
    check(r) {
      assert.strictEqual(r.extractor, "site:wechat");
      assert.strictEqual(r.articleTitle, "为什么个人知识库要围绕项目组织");
      assert.strictEqual(r.byline, "知织笔记");
      assert.ok(r.textContent.includes("项目是最自然的组织单元"));
      assert.ok(r.textContent.includes("[图片：项目结构示意图]"));
      assert.ok(!r.textContent.includes("扫一扫"));
      assert.ok(!r.textContent.includes("脚本内容"));
    },
  },
  {
    name: "知乎专栏（站点规则）",
    url: "https://zhuanlan.zhihu.com/p/123456",
    html: fixture("zhuanlan.html"),
    check(r) {
      assert.strictEqual(r.extractor, "site:zhihu-zhuanlan");
      assert.strictEqual(r.articleTitle, "pgvector 混合检索入门");
      assert.strictEqual(r.byline, "李四");
      assert.ok(r.textContent.includes("## 中文分词"));
      assert.ok(!r.textContent.includes("广告内容"));
    },
  },
  {
    name: "超长正文截断",
    url: "https://blog.example.com/long",
    html: `<!doctype html><html><head><title>长文</title></head><body><article><h1>长文</h1>${"<p>" + "中文段落内容。".repeat(400) + "</p>"}`.repeat(1) + "<p>" + "补充".repeat(60000) + "</p></article></body></html>",
    check(r) {
      assert.strictEqual(r.truncated, true);
      assert.strictEqual(r.textLength, 100000);
      assert.ok(r.originalTextLength > 100000);
    },
  },
  {
    name: "空白页回退",
    url: "https://blog.example.com/empty",
    html: "<!doctype html><html><head><title>空</title></head><body><div>只有一句很短的话。</div></body></html>",
    check(r) {
      assert.strictEqual(r.title, "空");
      assert.ok(typeof r.textContent === "string");
    },
  },
];

function assertPlistSafe(r) {
  for (const [k, v] of Object.entries(r)) {
    assert.ok(v !== null && v !== undefined, `${k} 不能为 null`);
    assert.ok(["string", "number", "boolean"].includes(typeof v), `${k} 类型为 ${typeof v}`);
  }
}

(async () => {
  const browser = await chromium.launch();
  let failed = 0;
  try {
    for (const c of cases) {
      const page = await browser.newPage();
      await page.route("**/*", (route) =>
        route.request().url() === c.url
          ? route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: c.html })
          : route.fulfill({ status: 404, body: "" })
      );
      await page.goto(c.url);
      await page.addScriptTag({ content: bundle });
      const r = await page.evaluate(
        () => new Promise((resolve) => ExtensionPreprocessingJS.run({ completionFunction: resolve }))
      );
      try {
        assertPlistSafe(r);
        c.check(r);
        console.log(`✓ ${c.name}  extractor=${r.extractor} textLength=${r.textLength} elapsedMs=${r.elapsedMs}`);
      } catch (e) {
        failed++;
        console.log(`✗ ${c.name}: ${e.message}`);
        console.log(JSON.stringify(r, null, 2).slice(0, 2000));
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
  process.exit(failed ? 1 : 0);
})();
