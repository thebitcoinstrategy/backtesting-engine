/* Shared navigation JavaScript — extracted from inline templates.
   Loaded by all pages that include the navbar (NAV_HTML). */

// Theme toggle
(function() {
    var saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    var inp = document.getElementById('theme-input');
    if (inp) inp.value = saved;
})();
function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme') || 'dark';
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    var inp = document.getElementById('theme-input');
    if (inp) inp.value = next;
}
var _swal = Swal.mixin({
    background: '#1e2130', color: '#e8e9ed', confirmButtonColor: '#6495ED',
    customClass: { popup: 'swal-dark' }
});
// Notification bell
function toggleNotifDropdown(e) {
    e.stopPropagation();
    var dd = document.getElementById('notif-dropdown');
    if (!dd) return;
    var wasHidden = dd.classList.contains('hidden');
    dd.classList.toggle('hidden');
    var add = document.getElementById('avatar-dropdown');
    if (add) add.classList.add('hidden');
    if (wasHidden) {
        // Auto-mark as read when opening
        fetch('/api/notifications/read', { method: 'POST' });
        var badge = document.getElementById('notif-badge');
        if (badge) badge.classList.add('hidden');
    }
}
function toggleAvatarDropdown(e) {
    e.stopPropagation();
    var dd = document.getElementById('avatar-dropdown');
    if (dd) dd.classList.toggle('hidden');
    var ndd = document.getElementById('notif-dropdown');
    if (ndd) ndd.classList.add('hidden');
}
document.addEventListener('click', function(e) {
    var dd = document.getElementById('notif-dropdown');
    if (dd && !dd.classList.contains('hidden')) {
        var wrap = document.querySelector('.notif-bell-wrap');
        if (wrap && !wrap.contains(e.target)) dd.classList.add('hidden');
    }
    var add = document.getElementById('avatar-dropdown');
    if (add && !add.classList.contains('hidden')) {
        var awrap = document.querySelector('.avatar-wrap');
        if (awrap && !awrap.contains(e.target)) add.classList.add('hidden');
    }
});
function fetchNotifications() {
    fetch('/api/notifications').then(function(r) { return r.json(); })
    .then(function(data) {
        var badge = document.getElementById('notif-badge');
        var list = document.getElementById('notif-list');
        if (!badge || !list) return;
        if (data.count > 0) {
            badge.textContent = data.count > 99 ? '99+' : data.count;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
        if (data.notifications.length === 0) {
            list.innerHTML = '<div class="notif-empty">No notifications yet</div>';
        } else {
            list.innerHTML = data.notifications.map(function(n) {
                var text, href;
                var readClass = n.is_read ? 'notif-read' : 'notif-unread';
                if (n.type === 'welcome') {
                    text = _escHtml(n.message || 'Welcome!');
                    href = n.link || '/feedback';
                } else {
                    var title = n.backtest_title || 'Untitled';
                    if (title.length > 40) title = title.substring(0, 37) + '...';
                    text = n.type === 'reply'
                        ? '<strong>' + _escHtml(n.actor_name) + '</strong> replied to your comment on <em>' + _escHtml(title) + '</em>'
                        : '<strong>' + _escHtml(n.actor_name) + '</strong> commented on your backtest <em>' + _escHtml(title) + '</em>';
                    href = '/backtest/' + n.backtest_id + '#comment-' + n.comment_id;
                }
                return '<a class="notif-item ' + readClass + '" href="' + href + '">'
                    + '<div class="notif-item-text">' + text + '</div>'
                    + '<div class="notif-item-time">' + _escHtml(n.time_ago) + '</div></a>';
            }).join('');
        }
    }).catch(function() {});
}
function _escHtml(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
document.addEventListener('DOMContentLoaded', function() {
    if (document.getElementById('notif-badge')) fetchNotifications();
});
