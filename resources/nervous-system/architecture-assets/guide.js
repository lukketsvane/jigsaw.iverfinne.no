(() => {
  "use strict";
  const ns = "http://www.w3.org/2000/svg";
  const plot = document.querySelector("#seed-plot");
  const slider = document.querySelector("#seed-position");
  const points = [[53,48],[146,36],[263,38],[382,61],[78,138],[201,113],[313,127],[429,155],[37,207],[145,197],[251,205],[353,211]];
  const active = 5;
  const width = 460;
  const height = 235;

  // Clip a candidate cell to the half-plane nearer to its seed than its neighbor.
  function bisect(polygon, seed, neighbor) {
    const dx = neighbor[0] - seed[0];
    const dy = neighbor[1] - seed[1];
    const offset = (neighbor[0] ** 2 + neighbor[1] ** 2 - seed[0] ** 2 - seed[1] ** 2) / 2;
    const distance = ([x,y]) => dx*x + dy*y - offset;
    const clipped = [];
    polygon.forEach((end, i) => {
      const start = polygon[(i + polygon.length - 1) % polygon.length];
      const a = distance(start), b = distance(end);
      if ((a <= 0) !== (b <= 0)) {
        const t = a / (a - b);
        clipped.push([start[0] + t*(end[0]-start[0]), start[1] + t*(end[1]-start[1])]);
      }
      if (b <= 0) clipped.push(end);
    });
    return clipped;
  }

  function element(name, attrs, text) {
    const el = document.createElementNS(ns, name);
    for (const [key,value] of Object.entries(attrs)) el.setAttribute(key, value);
    if (text) el.textContent = text;
    return el;
  }

  function drawCells() {
    points[active][0] = Number(slider.value);
    const cells = points.map((seed, i) => {
      let cell = [[0,0],[width,0],[width,height],[0,height]];
      points.forEach((neighbor,j) => {
        if (i !== j) cell = bisect(cell,seed,neighbor);
      });
      return element("polygon", {points:cell.map(pt => pt.map(v => v.toFixed(2)).join(",")).join(" "), class:i===active?"active-cell":"cell"});
    });
    const selected = cells.splice(active,1)[0];
    const sideCount = selected.getAttribute("points").split(" ").length;
    slider.setAttribute("aria-valuetext", `Seed A at ${Math.round(points[active][0] / width * 100)} percent of the board width. Its cell has ${sideCount} sides.`);
    const dots = points.map(([cx,cy],i) => element("circle",{cx,cy,r:i===active?5.5:3.1,class:i===active?"seed active-seed":"seed"}));
    plot.replaceChildren(...cells, selected, ...dots, element("text",{x:points[active][0]+11,y:points[active][1]-7,class:"seed-label"},"A"));
  }

  let dragPointer = null;
  function drag(event) {
    if (dragPointer !== event.pointerId) return;
    const p = new DOMPoint(event.clientX,event.clientY).matrixTransform(plot.getScreenCTM().inverse());
    slider.value = String(Math.max(Number(slider.min),Math.min(Number(slider.max),Math.round(p.x))));
    drawCells();
  }
  plot.addEventListener("pointerdown",event => {
    if (!event.target.classList.contains("active-seed")) return;
    dragPointer = event.pointerId;
    plot.ownerSVGElement.setPointerCapture(dragPointer);
  });
  plot.ownerSVGElement.addEventListener("pointermove",drag);
  plot.ownerSVGElement.addEventListener("lostpointercapture",() => { dragPointer = null; });
  slider.addEventListener("input",drawCells);
  drawCells();
  document.querySelector(".seed-controls").hidden = false;

  const tabs = [...document.querySelectorAll(".cut-tabs a")];
  const panels = [...document.querySelectorAll(".cut-panel")];
  function selectCut(tab, updateAddress) {
    const id = tab.hash.slice(1);
    for (const panel of panels) panel.hidden = panel.id !== id;
    document.querySelector(".thermal").hidden = !["cut-dendrite","cut-amoeba"].includes(id);
    for (const figure of document.querySelectorAll(".studio-plate")) figure.hidden = figure.dataset.cut !== id;
    for (const item of tabs) {
      const chosen = item === tab;
      item.setAttribute("aria-selected", String(chosen));
      item.tabIndex = chosen ? 0 : -1;
    }
    if (updateAddress) history.replaceState(null, "", tab.hash);
  }
  const nav = document.querySelector(".cut-tabs");
  nav.setAttribute("role","tablist");
  tabs.forEach((tab,i) => {
    tab.id = "tab-" + tab.hash.slice(1);
    tab.setAttribute("role","tab");
    tab.setAttribute("aria-controls",tab.hash.slice(1));
    const panel=document.getElementById(tab.hash.slice(1));
    panel.setAttribute("role","tabpanel");
    panel.setAttribute("aria-labelledby",tab.id);
    panel.tabIndex=0;
    tab.addEventListener("click",event => {
      event.preventDefault();
      selectCut(tab,true);
    });
    tab.addEventListener("keydown",event => {
      const next = {ArrowLeft:(i+tabs.length-1)%tabs.length,ArrowRight:(i+1)%tabs.length,Home:0,End:tabs.length-1}[event.key];
      if (next === undefined) return;
      event.preventDefault();
      tabs[next].focus();
      selectCut(tabs[next],true);
    });
  });
  const cutFromAddress = () => tabs.find(tab => tab.hash === location.hash);
  selectCut(cutFromAddress() || tabs[0],false);
  function revealLinkedCut() {
    const tab = cutFromAddress();
    if (tab) { selectCut(tab,false); nav.scrollIntoView({block:"start"}); }
  }
  if (cutFromAddress()) requestAnimationFrame(revealLinkedCut);
  window.addEventListener("hashchange",revealLinkedCut);

  let closedDetails;
  window.addEventListener("beforeprint",() => {
    if (!closedDetails) closedDetails = [...document.querySelectorAll("details:not([open])")];
    for (const item of closedDetails) item.open = true;
  });
  window.addEventListener("afterprint",() => {
    for (const item of closedDetails || []) item.open = false;
    closedDetails = null;
  });
})();
