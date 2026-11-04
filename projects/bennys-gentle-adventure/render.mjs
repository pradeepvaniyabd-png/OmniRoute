#!/usr/bin/env node
// Renders scene.html frame-by-frame with headless Chromium and encodes 1080p video segments.
//   node render.mjs                -> full render ($OUT/video.mp4, silent)
//   node render.mjs --stills 3,40  -> PNG stills at given seconds (QA)
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = process.env.OUT || path.join(HERE, "out");
const FPS = Number(process.env.FPS || 30);
const WORKERS = Number(process.env.WORKERS || 4);
const timeline = JSON.parse(readFileSync(path.join(OUT, "timeline.json"), "utf8"));

async function openPage(browser) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  page.on("pageerror", (e) => {
    console.error("page error:", e.message);
    process.exitCode = 1;
  });
  await page.goto(pathToFileURL(path.join(HERE, "scene.html")).href);
  await page.evaluate((tl) => window.setTimeline(tl), timeline);
  await page.evaluate(() => window.fontsReady());
  return page;
}

function ffmpegSegment(file) {
  const ff = spawn(
    "ffmpeg",
    ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "png", "-i", "-",
      "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-tune", "animation",
      "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", String(FPS), file],
    { stdio: ["pipe", "inherit", "inherit"] }
  );
  const done = new Promise((res, rej) => ff.on("close", (c) => (c === 0 ? res() : rej(new Error(`ffmpeg exit ${c}`)))));
  return { ff, done };
}

async function renderRange(browser, idx, f0, f1, segFile) {
  const page = await openPage(browser);
  const { ff, done } = ffmpegSegment(segFile);
  for (let f = f0; f < f1; f++) {
    await page.evaluate((t) => window.renderFrame(t), f / FPS);
    const buf = await page.screenshot({ type: "png", clip: { x: 0, y: 0, width: 1920, height: 1080 } });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    if ((f - f0) % 300 === 0) console.log(`  worker ${idx}: frame ${f - f0}/${f1 - f0}`);
  }
  ff.stdin.end();
  await done;
  await page.close();
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  try {
    const si = process.argv.indexOf("--stills");
    if (si > 0) {
      const page = await openPage(browser);
      for (const s of process.argv[si + 1].split(",").map(Number)) {
        await page.evaluate((t) => window.renderFrame(t), s);
        const f = path.join(OUT, `still_${String(s).replace(".", "_")}.png`);
        await page.screenshot({ path: f });
        console.log(f);
      }
      return;
    }
    const total = Math.ceil(timeline.total * FPS);
    const per = Math.ceil(total / WORKERS);
    const segs = [];
    const jobs = [];
    for (let w = 0; w < WORKERS; w++) {
      const f0 = w * per, f1 = Math.min(total, f0 + per);
      if (f0 >= f1) break;
      const seg = path.join(OUT, `seg_${w}.mp4`);
      segs.push(seg);
      jobs.push(renderRange(browser, w, f0, f1, seg));
    }
    console.log(`Rendering ${total} frames @${FPS}fps with ${jobs.length} workers...`);
    await Promise.all(jobs);
    const list = path.join(OUT, "segments.txt");
    writeFileSync(list, segs.map((s) => `file '${s}'`).join("\n"));
    await new Promise((res, rej) => {
      const p = spawn("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list, "-c", "copy", path.join(OUT, "video.mp4")], { stdio: "inherit" });
      p.on("close", (c) => (c === 0 ? res() : rej(new Error("concat failed"))));
    });
    if (!existsSync(path.join(OUT, "video.mp4"))) throw new Error("video.mp4 missing");
    console.log("video.mp4 ready");
  } finally {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
