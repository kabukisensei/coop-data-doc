document.addEventListener("DOMContentLoaded", () => {
    const tooltip = document.getElementById("estate-tooltip");
    if (!tooltip) return;

    document.querySelectorAll(".estate-node").forEach(node => {
        node.addEventListener("mouseenter", (e) => {
            const title = node.getAttribute("data-title");
            const tables = node.getAttribute("data-tables");
            const views = node.getAttribute("data-views");
            const procs = node.getAttribute("data-procs");
            const warnings = node.getAttribute("data-warnings");
            
            const heading = document.createElement("strong");
            heading.textContent = title;
            tooltip.replaceChildren(heading);
            for (const line of [`Tables: ${tables}`, `Views: ${views}`,
                                `Procs: ${procs}`, `Warnings: ${warnings}`]) {
                tooltip.append(document.createElement("br"), document.createTextNode(line));
            }
            tooltip.style.display = "block";
        });
        
        node.addEventListener("mousemove", (e) => {
            tooltip.style.left = (e.pageX + 15) + "px";
            tooltip.style.top = (e.pageY + 15) + "px";
        });
        
        node.addEventListener("mouseleave", () => {
            tooltip.style.display = "none";
        });
        
        node.style.cursor = "pointer";
    });
});
