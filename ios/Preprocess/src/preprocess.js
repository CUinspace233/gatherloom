/*
 * Gatherloom Safari 分享扩展 JavaScript 预处理（ADR 0004）
 *
 * Safari 在用户点击分享时，在当前页面中执行本文件，并把 completionFunction 的参数
 * 作为 NSExtensionJavaScriptPreprocessingResultsKey 交给分享扩展。
 * 返回值只能包含属性列表类型（字符串、数字、布尔、数组、字典），不能有 null。
 *
 * 构建：scripts/s0/build_preprocess_js.sh 把 vendor/Readability.js 与本文件拼接为
 * ios/ShareExtension/Resources/GatherloomPreprocess.js。
 */

var GATHERLOOM_PREPROCESS_VERSION = 1;
var GATHERLOOM_MAX_TEXT = 100000;

// 站点规则：在 Readability 之前尝试，命中且正文足够长时直接使用。
var GatherloomSiteRules = [
  {
    name: "wechat",
    host: /(^|\.)mp\.weixin\.qq\.com$/,
    content: "#js_content",
    title: "#activity-name",
    byline: "#js_name"
  },
  {
    name: "zhihu-zhuanlan",
    host: /(^|\.)zhuanlan\.zhihu\.com$/,
    content: ".Post-RichText",
    title: ".Post-Title",
    byline: ".AuthorInfo-name"
  },
  {
    name: "zhihu-answer",
    host: /(^|\.)zhihu\.com$/,
    path: /\/answer\/\d+/,
    content: ".QuestionAnswer-content .RichContent-inner, .AnswerCard .RichContent-inner",
    title: ".QuestionHeader-title",
    byline: ".AuthorInfo-name"
  }
];

var GATHERLOOM_SKIP_TAGS = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|SVG|IFRAME|BUTTON|INPUT|SELECT|TEXTAREA|CANVAS)$/;
var GATHERLOOM_BLOCK_TAGS = /^(P|DIV|SECTION|ARTICLE|MAIN|UL|OL|TABLE|TR|BLOCKQUOTE|PRE|FIGURE|FIGCAPTION|HEADER|FOOTER|ASIDE|DL|DT|DD|HR)$/;

// 把 DOM 转为保留段落、标题（# 前缀）、列表（- 前缀）和代码缩进的纯文本。
function gatherloomNodeToText(root) {
  var out = [];

  function lastChar() {
    for (var i = out.length - 1; i >= 0; i--) {
      if (out[i].length) return out[i].charAt(out[i].length - 1);
    }
    return "\n";
  }

  function push(text) {
    out.push(text);
  }

  function walk(node, inPre) {
    if (node.nodeType === 3) {
      var t = node.nodeValue || "";
      if (inPre) {
        push(t);
      } else {
        t = t.replace(/[\s ]+/g, " ");
        if (lastChar() === "\n" || lastChar() === " ") t = t.replace(/^ /, "");
        if (t) push(t);
      }
      return;
    }
    if (node.nodeType !== 1) return;

    var tag = node.tagName.toUpperCase();
    if (GATHERLOOM_SKIP_TAGS.test(tag)) return;
    if (node.hasAttribute && node.hasAttribute("hidden")) return;
    if (node.style && node.style.display === "none") return;
    if (tag === "BR") { push("\n"); return; }
    if (tag === "IMG") {
      var alt = (node.getAttribute("alt") || "").trim();
      if (alt) push("[图片：" + alt + "]");
      return;
    }

    var heading = /^H([1-6])$/.exec(tag);
    var isBlock = GATHERLOOM_BLOCK_TAGS.test(tag);
    var isPre = inPre || tag === "PRE";

    if (heading) {
      push("\n\n" + new Array(Number(heading[1]) + 1).join("#") + " ");
    } else if (tag === "LI") {
      push(lastChar() === "\n" ? "- " : "\n- ");
    } else if (isBlock) {
      push("\n\n");
    }

    for (var c = node.firstChild; c; c = c.nextSibling) walk(c, isPre);

    if (heading || isBlock) push("\n\n");
    else if (tag === "LI") push("\n");
  }

  walk(root, false);

  return out
    .join("")
    .replace(/[ \t ]+\n/g, "\n")
    .replace(/\n- \n+/g, "\n- ")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/^\n+|\s+$/g, "");
}

function gatherloomHTMLToText(html) {
  var doc = document.implementation.createHTMLDocument("");
  doc.body.innerHTML = html;
  return gatherloomNodeToText(doc.body);
}

function gatherloomQueryText(doc, selector) {
  if (!selector) return undefined;
  var node = doc.querySelector(selector);
  if (!node) return undefined;
  var text = (node.textContent || "").replace(/\s+/g, " ").trim();
  return text || undefined;
}

function gatherloomExtractBySiteRule(doc, loc) {
  for (var i = 0; i < GatherloomSiteRules.length; i++) {
    var rule = GatherloomSiteRules[i];
    if (!rule.host.test(loc.hostname)) continue;
    if (rule.path && !rule.path.test(loc.pathname)) continue;
    var node = doc.querySelector(rule.content);
    if (!node) continue;
    var text = gatherloomNodeToText(node);
    if (text.length < 20) continue;
    return {
      extractor: "site:" + rule.name,
      textContent: text,
      articleTitle: gatherloomQueryText(doc, rule.title),
      byline: gatherloomQueryText(doc, rule.byline)
    };
  }
  return null;
}

// 只保留属性列表可接受的值；丢弃 null / undefined。
function gatherloomPlistSafe(obj) {
  var result = {};
  for (var key in obj) {
    if (!Object.prototype.hasOwnProperty.call(obj, key)) continue;
    var v = obj[key];
    if (v === null || v === undefined) continue;
    if (typeof v === "number") {
      if (isFinite(v)) result[key] = v;
    } else if (typeof v === "boolean") {
      result[key] = v;
    } else {
      result[key] = String(v);
    }
  }
  return result;
}

var GatherloomPreprocessor = function () {};

GatherloomPreprocessor.prototype.extract = function () {
  var started = Date.now();
  var r = {
    preprocessVersion: GATHERLOOM_PREPROCESS_VERSION,
    url: String(document.URL || ""),
    title: String(document.title || ""),
    capturedAt: new Date().toISOString()
  };

  try {
    var canonical = document.querySelector('link[rel="canonical"]');
    if (canonical && canonical.href) r.canonicalURL = canonical.href;
    var lang = document.documentElement.getAttribute("lang");
    if (lang) r.lang = lang;
    r.htmlLength = document.documentElement.outerHTML.length;

    var site = gatherloomExtractBySiteRule(document, window.location);
    if (site) {
      r.extractor = site.extractor;
      r.textContent = site.textContent;
      r.articleTitle = site.articleTitle;
      r.byline = site.byline;
    } else if (typeof Readability === "function") {
      var article = new Readability(document.cloneNode(true), { charThreshold: 300 }).parse();
      if (article && article.content) {
        r.extractor = "readability-0.6.0";
        r.textContent = gatherloomHTMLToText(article.content);
        r.articleTitle = article.title || undefined;
        r.byline = article.byline || undefined;
        r.siteName = article.siteName || undefined;
        r.excerpt = article.excerpt || undefined;
        r.publishedTime = article.publishedTime || undefined;
        if (!r.lang && article.lang) r.lang = article.lang;
      }
    }
  } catch (e) {
    r.error = String((e && e.message) || e);
  }

  if (!r.textContent || r.textContent.length < 50) {
    try {
      var bodyText = document.body ? gatherloomNodeToText(document.body) : "";
      if (bodyText.length > (r.textContent || "").length) {
        r.extractor = "body-fallback";
        r.textContent = bodyText;
      }
    } catch (e2) {
      r.error = (r.error ? r.error + "; " : "") + String((e2 && e2.message) || e2);
    }
  }

  var text = r.textContent || "";
  r.originalTextLength = text.length;
  r.truncated = text.length > GATHERLOOM_MAX_TEXT;
  if (r.truncated) text = text.slice(0, GATHERLOOM_MAX_TEXT);
  r.textContent = text;
  r.textLength = text.length;
  r.elapsedMs = Date.now() - started;

  return gatherloomPlistSafe(r);
};

GatherloomPreprocessor.prototype.run = function (args) {
  args.completionFunction(this.extract());
};

GatherloomPreprocessor.prototype.finalize = function () {};

var ExtensionPreprocessingJS = new GatherloomPreprocessor();
