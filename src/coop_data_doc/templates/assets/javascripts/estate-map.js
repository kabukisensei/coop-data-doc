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
            
            tooltip.innerHTML = `<strong>${title}</strong><br/>
            Tables: ${tables}<br/>
            Views: ${views}<br/>
            Procs: ${procs}<br/>
            Warnings: ${warnings}`;
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
