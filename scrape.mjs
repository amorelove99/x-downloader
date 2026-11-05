// Runs inside `ego-browser nodejs` (stdin). xdl.py / runner.py fills limits and temporary output paths.
const MAX = __MAX__;
const ACCOUNT_ONLY = typeof __ACCOUNT_ONLY__ !== "undefined" ? __ACCOUNT_ONLY__ : false;
const OUT = "__OUT__";
const PROGRESS = __PROGRESS__;
const KNOWN = typeof __KNOWN__ !== "undefined" ? __KNOWN__ : {};
const STOP_AFTER = 20;
const fs = await import("node:fs/promises");
const progress = (message) => fs.appendFile(PROGRESS, message + "\n");

const task = await taskSpace("x-bookmarks-sync");
const page = task.page("p1");
const t0 = Date.now();

const write = (obj) => fs.writeFile(OUT, JSON.stringify({ ...obj, elapsedMs: Date.now() - t0 }, null, 1));

await page.goto("https://x.com/i/bookmarks", { timeout: 45000 });
await page.waitForTimeout(2500);

const url = await page.url();
if (/\/(login|i\/flow)/.test(url)) {
  await write({ status: "login_required", url, items: [] });
  console.log("login_required");
} else {
  const account = await page.evaluate(() => {
    const switcher = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
    const handles = switcher?.innerText.match(/@[A-Za-z0-9_]{1,15}/g);
    if (handles?.length === 1) return handles[0].slice(1);
    const profile = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
    const path = profile?.getAttribute("href");
    return path && /^\/[A-Za-z0-9_]{1,15}$/.test(path) ? path.slice(1) : null;
  });

  if (!account) {
    await write({ status: "account_unknown", url, items: [] });
    throw new Error("无法识别当前登录账号；未从收藏作者推断账号");
  }
  await progress(`[账号] 当前登录 @${account}`);

  if (ACCOUNT_ONLY) {
    await write({ status: "ok", account, count: 0, scrolls: 0, items: [] });
    await task.finish({ keep: [] });
  } else {
    const scraped = await page.evaluate(async ({ maxItems, known, stopAfter }) => {
      const ct0 = document.cookie
        .split(";")
        .map((s) => s.trim())
        .find((s) => s.startsWith("ct0="))
        ?.slice(4);
      if (!ct0) return { error: "login_required" };

      const bearer = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
      const headers = {
        "Authorization": "Bearer " + decodeURIComponent(bearer),
        "X-Csrf-Token": ct0,
        "X-Twitter-Auth-Type": "OAuth2Session",
        "X-Twitter-Active-User": "yes"
      };

      let queryId = "Fy0QMy4q_aZCpkO0PnyLYw";
      const features = {
        rweb_video_screen_enabled: false,
        profile_label_improvements_pcf_label_in_post_enabled: true,
        responsive_web_profile_redirect_enabled: false,
        rweb_tipjar_consumption_enabled: false,
        verified_phone_label_enabled: false,
        creator_subscriptions_tweet_preview_api_enabled: true,
        responsive_web_graphql_timeline_navigation_enabled: true,
        responsive_web_graphql_skip_user_profile_image_extensions_enabled: false,
        premium_content_api_read_enabled: false,
        communities_web_enable_tweet_community_results_fetch: true,
        c9s_tweet_anatomy_moderator_badge_enabled: true,
        articles_preview_enabled: true,
        responsive_web_edit_tweet_api_enabled: true,
        graphql_is_translatable_rweb_tweet_is_translatable_enabled: true,
        view_counts_everywhere_api_enabled: true,
        longform_notetweets_consumption_enabled: true,
        responsive_web_twitter_article_tweet_consumption_enabled: true,
        tweet_awards_web_tipping_enabled: false,
        content_disclosure_indicator_enabled: true,
        content_disclosure_ai_generated_indicator_enabled: true,
        freedom_of_speech_not_reach_fetch_enabled: true,
        standardized_nudges_misinfo: true,
        tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled: true,
        longform_notetweets_rich_text_read_enabled: true,
        longform_notetweets_inline_media_enabled: false,
        responsive_web_enhance_cards_enabled: false
      };

      const scriptUrls = Array.from(
        new Set(
          Array.from(document.scripts)
            .map((s) => s.src)
            .filter(Boolean)
            .concat(
              performance
                .getEntriesByType("resource")
                .map((r) => r.name)
                .filter((u) => u.includes("client-web") && u.endsWith(".js"))
            )
        )
      );

      for (const sUrl of scriptUrls) {
        const r = await fetch(sUrl);
        if (!r.ok) continue;
        const txt = await r.text();
        const mA = txt.match(/queryId:"([A-Za-z0-9_-]+)"[^}]{0,400}operationName:"Bookmarks"/);
        const mB = !mA && txt.match(/operationName:"Bookmarks"[^}]{0,400}queryId:"([A-Za-z0-9_-]+)"/);
        const m = mA || mB;
        if (m) {
          queryId = m[1];
          const idx = m.index || 0;
          const win = txt.slice(Math.max(0, idx - 500), Math.min(txt.length, idx + 1500));
          const sw = win.match(/featureSwitches:\[([^\]]*)\]/)?.[1];
          if (sw) {
            for (const x of sw.matchAll(/"([^"]+)"/g)) {
              if (!(x[1] in features)) features[x[1]] = true;
            }
          }
          break;
        }
      }

      function normalizeImageUrl(rawUrl) {
        try {
          const u = new URL(rawUrl);
          if (u.protocol !== "https:" || u.hostname !== "pbs.twimg.com" || !u.pathname.startsWith("/media/")) {
            return null;
          }
          const extMatch = u.pathname.match(/\.([A-Za-z0-9]+)$/);
          const ext = (u.searchParams.get("format") || (extMatch ? extMatch[1] : "jpg")).toLowerCase();
          if (!["jpg", "jpeg", "png", "webp", "gif"].includes(ext)) return null;
          const cleanPath = u.pathname.replace(/\.[A-Za-z0-9]+$/, "");
          return `https://pbs.twimg.com${cleanPath}?format=${ext}&name=orig`;
        } catch {
          return null;
        }
      }

      function parseMediaEntities(mediaArray, videoMap, imageSet) {
        for (const m of mediaArray || []) {
          if (!m) continue;
          if (m.type === "video" || m.type === "animated_gif") {
            const vidId = String(m.id_str || m.media_key || "");
            const variants = (m.video_info?.variants || [])
              .filter((v) => v?.content_type === "video/mp4" && v?.url)
              .map((v) => {
                const dim = v.url.match(/\/(\d+)x(\d+)\//);
                return {
                  url: v.url,
                  bitrate: v.bitrate || 0,
                  width: dim ? Number(dim[1]) : 0,
                  height: dim ? Number(dim[2]) : 0
                };
              })
              .sort((a, b) => b.bitrate - a.bitrate);
            if (variants.length > 0) {
              videoMap.set(vidId || variants[0].url, {
                id: vidId || String(videoMap.size + 1),
                variants
              });
            }
          } else if (m.type === "photo" && m.media_url_https) {
            const norm = normalizeImageUrl(m.media_url_https);
            if (norm) imageSet.add(norm);
          }
        }
      }

      function extractTweet(rawResult, seen) {
        if (!rawResult) return null;
        const tw = rawResult.tweet || rawResult;
        if (!tw.rest_id || !/^\d+$/.test(String(tw.rest_id)) || seen.has(tw.rest_id)) return null;

        const legacy = tw.legacy || {};
        const user = tw.core?.user_results?.result;
        const author = user?.legacy?.screen_name || user?.core?.screen_name || "";
        if (!/^[A-Za-z0-9_]{1,15}$/.test(author)) return null;
        seen.add(tw.rest_id);

        const videoMap = new Map();
        const imageSet = new Set();

        // 1. Direct media
        parseMediaEntities(legacy.extended_entities?.media || legacy.entities?.media || [], videoMap, imageSet);

        // 2. unified_card media_entities
        const bvs = tw.card?.legacy?.binding_values || [];
        const ucRaw = bvs.find((b) => b?.key === "unified_card")?.value?.string_value;
        if (ucRaw) {
          try {
            const uc = JSON.parse(ucRaw);
            parseMediaEntities(Object.values(uc.media_entities || {}), videoMap, imageSet);
          } catch {}
        }

        // 3. Quoted tweet media
        const quotedRaw = tw.quoted_status_result?.result || legacy.quoted_status_result?.result;
        const quotedTw = quotedRaw?.tweet || quotedRaw;
        if (quotedTw) {
          const qLegacy = quotedTw.legacy || {};
          parseMediaEntities(qLegacy.extended_entities?.media || qLegacy.entities?.media || [], videoMap, imageSet);
        }

        // 4. Article / card / nested pbs.twimg.com/media/ images
        const rawJson = JSON.stringify(tw);
        for (const m of rawJson.matchAll(/https:\/\/pbs\.twimg\.com\/media\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9]+)?(?:\?[^"\\]*)?/g)) {
          const norm = normalizeImageUrl(m[0]);
          if (norm) imageSet.add(norm);
        }

        const videos = [...videoMap.values()];
        const images = [...imageSet];
        const createdAt = legacy.created_at ? new Date(legacy.created_at).toISOString() : new Date().toISOString();

        return {
          id: String(tw.rest_id),
          url: `https://x.com/${author}/status/${tw.rest_id}`,
          author,
          time: createdAt,
          hasVideo: videos.length > 0,
          images,
          videos
        };
      }

      const seen = new Set();
      const knownIds = new Set(known);
      let consecutiveKnown = 0;
      let caughtUp = false;
      const items = [];
      let cursor = null;
      let pages = 0;

      while (pages < 200 && items.length < maxItems && !caughtUp) {
        pages += 1;
        const count = Math.min(100, Math.max(20, maxItems - items.length));
        const vars = { count, includePromotedContent: false };
        if (cursor) vars.cursor = cursor;

        const apiUrl =
          "/i/api/graphql/" +
          queryId +
          "/Bookmarks?variables=" +
          encodeURIComponent(JSON.stringify(vars)) +
          "&features=" +
          encodeURIComponent(JSON.stringify(features));

        const resp = await fetch(apiUrl, { headers, credentials: "include" });
        if (!resp.ok) {
          if (resp.status === 401 || resp.status === 403) return { error: "login_required" };
          throw new Error(`Bookmarks GraphQL HTTP ${resp.status}`);
        }
        const data = await resp.json();
        const instructions =
          data?.data?.bookmark_timeline_v2?.timeline?.instructions ||
          data?.data?.bookmark_timeline?.timeline?.instructions ||
          [];

        let nextCursor = null;
        for (const inst of instructions) {
          for (const entry of inst.entries || []) {
            const content = entry.content;
            if (
              content?.entryType === "TimelineTimelineCursor" ||
              content?.__typename === "TimelineTimelineCursor"
            ) {
              if (content.cursorType === "Bottom" || content.cursorType === "ShowMore") {
                nextCursor = content.value;
              }
              continue;
            }
            if (entry.entryId?.startsWith("cursor-bottom-") || entry.entryId?.startsWith("cursor-showMore-")) {
              nextCursor = content?.value || content?.itemContent?.value || nextCursor;
              continue;
            }

            const candidates = [];
            if (content?.itemContent?.tweet_results?.result) {
              candidates.push(content.itemContent.tweet_results.result);
            }
            for (const sub of content?.items || []) {
              if (sub?.item?.itemContent?.tweet_results?.result) {
                candidates.push(sub.item.itemContent.tweet_results.result);
              }
            }

            for (const c of candidates) {
              if (items.length >= maxItems || caughtUp) break;
              const parsed = extractTweet(c, seen);
              if (parsed) {
                items.push(parsed);
                consecutiveKnown = knownIds.has(parsed.id) ? consecutiveKnown + 1 : 0;
                if (stopAfter > 0 && consecutiveKnown >= stopAfter) caughtUp = true;
              }
            }
          }
        }

        if (!nextCursor || nextCursor === cursor || items.length >= maxItems || caughtUp) break;
        cursor = nextCursor;
      }

      return { pages, items, caughtUp };
    }, { maxItems: MAX, known: KNOWN[account.toLowerCase()] || [], stopAfter: STOP_AFTER });

    if (scraped.error === "login_required") {
      await write({ status: "login_required", url: await page.url(), items: [] });
      console.log("login_required");
    } else {
      await progress(`[采集] 完成，共 ${scraped.items.length} 条收藏（上限 ${MAX} 条，共请求 ${scraped.pages} 页${scraped.caughtUp ? `；已连续遇到 ${STOP_AFTER} 条已处理收藏，提前停止` : ""}）`);
      await write({
        status: "ok",
        account,
        count: scraped.items.length,
        scrolls: scraped.pages,
        items: scraped.items
      });
      await task.finish({ keep: [] });
    }
  }
}
