// Paste this into the browser console on a LinkedIn job search you already have open.
// It clicks each job already loaded on the page, reads the description, and downloads a file.
// Drop that file in the inbox folder, then run linkedin_jobs.py.
(async function collectLinkedInJobs() {
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function status(text) {
    let box = document.getElementById("job-collect-status");
    if (!box) {
      box = document.createElement("div");
      box.id = "job-collect-status";
      box.style.cssText = [
        "position:fixed",
        "bottom:16px",
        "right:16px",
        "z-index:2147483647",
        "background:#1c1917",
        "color:#fafaf9",
        "padding:12px 14px",
        "border-radius:10px",
        "max-width:280px",
        "font:15px/1.4 Georgia,serif",
        "box-shadow:0 8px 24px rgba(0,0,0,.25)",
      ].join(";");
      document.body.appendChild(box);
    }
    box.textContent = text;
  }

  function cards() {
    const seen = new Set();
    const found = [];
    for (const card of document.querySelectorAll('[componentkey^="job-card-component-ref-"]')) {
      const id = (card.getAttribute("componentkey") || "").replace("job-card-component-ref-", "");
      const box = card.getBoundingClientRect();
      if (!id || seen.has(id) || box.width < 40 || box.height < 20) continue;
      seen.add(id);
      found.push({ id, card });
    }
    return found;
  }

  function heading() {
    return [...document.querySelectorAll("h1, h2, h3, span, div")].find((node) => {
      const text = (node.innerText || "").trim();
      return text === "About the job" && node.childElementCount < 4;
    });
  }

  function description() {
    const node = heading();
    if (!node) return "";
    let box = node.parentElement;
    for (let i = 0; i < 8 && box; i += 1) {
      const text = (box.innerText || "").trim();
      if (text.startsWith("About the job") && text.length > 180) {
        return text.replace(/^About the job\s*/i, "").trim();
      }
      box = box.parentElement;
    }
    return "";
  }

  function linesFrom(card) {
    return (card.innerText || "")
      .split("\n")
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter((line) => line && !/^(easy apply|promoted|applied|viewed)$/i.test(line));
  }

  function fields(card) {
    const lines = [];
    for (const line of linesFrom(card)) {
      const clean = line.replace(/\s*\(Verified job\)\s*$/i, "");
      if (!lines.length) {
        lines.push(clean);
        continue;
      }
      if (lines[lines.length - 1] === clean || lines[lines.length - 1].startsWith(clean)) {
        lines[lines.length - 1] = clean;
        continue;
      }
      if (clean.startsWith(lines[lines.length - 1])) continue;
      lines.push(clean);
    }
    const title = lines[0] || "";
    const company = lines[1] || "";
    let location = "";
    for (const line of lines.slice(2, 6)) {
      if (/\bago\b|applicant|alumni/i.test(line)) continue;
      location = line;
      break;
    }
    return { title, company, location };
  }

  async function waitForJob(id, previous) {
    const start = Date.now();
    while (Date.now() - start < 8000) {
      const text = description();
      const open = new URL(location.href).searchParams.get("currentJobId");
      if (text && text !== previous && (open === id || text.length > 180)) return text;
      await pause(250);
    }
    return description();
  }

  const search = new URL(location.href).searchParams.get("keywords") || "";
  const list = cards();
  if (!list.length) {
    status("No job list found. Open a LinkedIn job search first, then run this again.");
    return;
  }

  const jobs = [];
  status("Reading " + list.length + " jobs already loaded on this page.");
  let previous = "";
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    status("Reading " + (i + 1) + " of " + list.length + "…");
    item.card.scrollIntoView({ block: "center" });
    item.card.click();
    const text = await waitForJob(item.id, previous);
    previous = text;
    const info = fields(item.card);
    jobs.push({
      id: item.id,
      title: info.title,
      company: info.company,
      location: info.location,
      description: text,
      url: "https://www.linkedin.com/jobs/view/" + item.id + "/",
      search,
    });
    await pause(900);
  }

  const payload = { search, jobs };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 10);
  link.href = URL.createObjectURL(blob);
  link.download = "linkedin-descriptions-" + stamp + ".json";
  link.click();
  status("Saved " + jobs.length + " descriptions. Put that file in the inbox folder and run the sorter.");
})();
