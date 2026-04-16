// admin_tab_persistence.js
// Keeps the selected admin tab active after page refresh using localStorage


document.addEventListener('DOMContentLoaded', function () {
    const adminTab = document.getElementById('adminTab');
    if (!adminTab) return;
    const tabButtons = adminTab.querySelectorAll('button[data-bs-toggle="tab"]');
    const lastTabId = localStorage.getItem('adminTabActiveId');
    if (lastTabId) {
        const lastTabBtn = document.getElementById(lastTabId);
        if (lastTabBtn) {
            new bootstrap.Tab(lastTabBtn).show();
        }
    }
    // Listen for tab changes
    tabButtons.forEach(btn => {
        btn.addEventListener('shown.bs.tab', function (e) {
            localStorage.setItem('adminTabActiveId', e.target.id);
        });
    });
});
