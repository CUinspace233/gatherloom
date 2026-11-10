// Runs inside Safari before the share sheet opens (ADR 0004). Reads the
// rendered page so sites behind login walls or JavaScript still yield text.
var GatherloomPreprocessor = function () {};

GatherloomPreprocessor.prototype = {
    run: function (args) {
        var result = { url: document.URL, title: document.title || "" };
        try {
            var canonical = document.querySelector("link[rel=canonical]");
            if (canonical && canonical.href) result.canonicalUrl = canonical.href;
            var heading = document.querySelector("meta[property='og:title']");
            if (heading && heading.content) result.title = heading.content;
            result.text = extractText();
        } catch (error) {
            result.text = "";
        }
        args.completionFunction(result);
    },
    finalize: function () {}
};

function extractText() {
    var root = pickRoot();
    var blocks = root.querySelectorAll("h1, h2, h3, h4, p, li, blockquote, pre, figcaption, section > span");
    var lines = [];
    var seen = new Set();
    for (var i = 0; i < blocks.length; i++) {
        var block = blocks[i];
        if (block.closest("nav, header, footer, aside, form, script, style, noscript")) continue;
        var text = (block.innerText || "").replace(/\s+\n/g, "\n").trim();
        if (!text || seen.has(text)) continue;
        seen.add(text);
        lines.push(text);
    }
    var joined = lines.join("\n\n");
    if (joined.length < 200) joined = (root.innerText || "").trim();
    return joined.slice(0, 100000);
}

function pickRoot() {
    // Common Chinese sources first: WeChat articles and Zhihu answers.
    var known = document.querySelector("#js_content, .RichText, .Post-RichText, article, main, [role=main]");
    if (known && (known.innerText || "").trim().length > 200) return known;
    var best = document.body;
    var bestScore = 0;
    var candidates = document.querySelectorAll("div, section");
    for (var i = 0; i < candidates.length; i++) {
        var node = candidates[i];
        var paragraphs = node.querySelectorAll(":scope > p");
        var score = 0;
        for (var j = 0; j < paragraphs.length; j++) score += (paragraphs[j].innerText || "").length;
        if (score > bestScore) {
            bestScore = score;
            best = node;
        }
    }
    return best;
}

var ExtensionPreprocessingJS = new GatherloomPreprocessor();
