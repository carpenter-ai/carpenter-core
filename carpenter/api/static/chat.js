/* Chat UI logic */
var UI_TOKEN = "__UI_TOKEN__";
var CONV_ID = __CONV_ID__;

function scrollToBottom() {
    var el = document.getElementById('chat-messages');
    el.scrollTop = el.scrollHeight;
}

function appendMessage(role, content) {
    var container = document.getElementById('chat-messages');
    var wrapper = document.createElement('div');
    wrapper.style.display = 'flex';
    wrapper.style.margin = '8px 0';

    var bubble = document.createElement('div');
    bubble.style.padding = '10px 14px';
    bubble.style.maxWidth = '75%';
    bubble.style.wordWrap = 'break-word';
    bubble.style.borderRadius = '12px';

    if (role === 'user') {
        wrapper.style.justifyContent = 'flex-end';
        bubble.style.background = '#2d4a22';
        bubble.style.color = '#a6e22e';
        bubble.style.borderBottomRightRadius = '2px';
    } else {
        wrapper.style.justifyContent = 'flex-start';
        bubble.style.background = '#3e3d32';
        bubble.style.color = '#f8f8f2';
        bubble.style.borderBottomLeftRadius = '2px';
    }

    bubble.innerHTML = content;
    wrapper.appendChild(bubble);
    container.appendChild(wrapper);
    scrollToBottom();
}

function renderContent(text) {
    // Split on fenced code blocks
    var parts = text.split(/(```\w*\n[\s\S]*?```)/g);
    var result = '';
    for (var i = 0; i < parts.length; i++) {
        var part = parts[i];
        var m = part.match(/^```\w*\n([\s\S]*?)```$/);
        if (m) {
            var code = m[1].replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            result += '<pre style="background:#1e1e1e;padding:12px;border-radius:4px;' +
                      'overflow-x:auto;font-size:13px;line-height:1.4;margin:8px 0;">' +
                      code + '</pre>';
        } else {
            var escaped = part.replace(/&/g,'&amp;').replace(/</g,'&lt;')
                              .replace(/>/g,'&gt;').replace(/\n/g,'<br>');
            // Linkify URLs
            escaped = escaped.replace(/(https?:\/\/[^\s<>&]+)/g,
                '<a href="$1" target="_blank" style="color:#66d9ef;text-decoration:underline;">$1</a>');
            escaped = escaped.replace(/(\/api\/review\/[a-f0-9-]+)/g,
                '<a href="$1" target="_blank" style="color:#66d9ef;text-decoration:underline;">$1</a>');
            result += escaped;
        }
    }
    return result;
}

var _pendingPoll = null;
var _optimisticId = 0;

function _appendOptimistic(text) {
    // Show the user's message immediately with a pending indicator
    var id = 'optimistic-' + (++_optimisticId);
    var container = document.getElementById('chat-messages');
    var wrapper = document.createElement('div');
    wrapper.id = id;
    wrapper.style.display = 'flex';
    wrapper.style.justifyContent = 'flex-end';
    wrapper.style.margin = '8px 0';

    var bubble = document.createElement('div');
    bubble.style.padding = '10px 14px';
    bubble.style.maxWidth = '75%';
    bubble.style.wordWrap = 'break-word';
    bubble.style.borderRadius = '12px 12px 2px 12px';
    bubble.style.background = '#2d4a22';
    bubble.style.color = '#a6e22e';
    bubble.style.position = 'relative';
    bubble.innerHTML = renderContent(text);

    var status = document.createElement('span');
    status.className = 'msg-status';
    status.style.cssText = 'display:inline-block;margin-left:8px;font-size:11px;opacity:0.6;vertical-align:middle;';
    status.textContent = '\u23f3';  // hourglass
    bubble.appendChild(status);

    wrapper.appendChild(bubble);
    container.appendChild(wrapper);
    scrollToBottom();
    return id;
}

function _markDelivered(optId) {
    var el = document.getElementById(optId);
    if (!el) return;
    var status = el.querySelector('.msg-status');
    if (status) {
        status.textContent = '\u2713';  // checkmark
        status.style.opacity = '0.5';
        status.style.color = '#a6e22e';
    }
}

function _markFailed(optId, errMsg) {
    var el = document.getElementById(optId);
    if (!el) return;
    var status = el.querySelector('.msg-status');
    if (status) {
        status.textContent = '\u2717';  // X mark
        status.style.opacity = '1';
        status.style.color = '#f92672';
        status.title = errMsg;
    }
}

async function sendMessage() {
    var input = document.getElementById('chat-input');
    var btn = document.getElementById('send-btn');
    var loading = document.getElementById('loading');
    var text = input.value.trim();
    if (!text) return;

    input.value = '';
    btn.disabled = true;
    loading.style.display = 'block';

    // Optimistic: show user message immediately
    var optId = _appendOptimistic(text);

    try {
        var hdrs = {'Content-Type': 'application/json'};
        if (UI_TOKEN) hdrs['Authorization'] = 'Bearer ' + UI_TOKEN;
        var resp = await fetch('/api/chat', {
            method: 'POST',
            headers: hdrs,
            body: JSON.stringify({text: text, user: 'default', conversation_id: CONV_ID})
        });
        if (!resp.ok) throw new Error('Server error ' + resp.status);
        // Mark as delivered
        _markDelivered(optId);
        // Poll for AI completion (will also refresh messages from server)
        _startPendingPoll();
    } catch (err) {
        _markFailed(optId, err.message);
        btn.disabled = false;
        loading.style.display = 'none';
        input.focus();
    }
}

function _refreshMessages() {
    var el = document.getElementById('chat-messages');
    htmx.ajax('GET', el.getAttribute('hx-get'), {target: '#chat-messages', swap: 'innerHTML'});
}

function _startPendingPoll() {
    // Poll /api/chat/pending every 500ms until the AI is done
    if (_pendingPoll) clearInterval(_pendingPoll);
    _pendingPoll = setInterval(async function() {
        try {
            var hdrs = {};
            if (UI_TOKEN) hdrs['Authorization'] = 'Bearer ' + UI_TOKEN;
            var resp = await fetch('/api/chat/pending?c=' + CONV_ID, {headers: hdrs});
            var data = await resp.json();
            if (!data.pending) {
                clearInterval(_pendingPoll);
                _pendingPoll = null;
                _refreshMessages();
                document.getElementById('send-btn').disabled = false;
                document.getElementById('loading').style.display = 'none';
                document.getElementById('chat-input').focus();
                // Refresh conversation list to pick up auto-generated title
                loadConversations();
            }
        } catch (e) {
            // Ignore transient fetch errors
        }
    }, 500);
}

function _tokenParam(sep) {
    return UI_TOKEN ? sep + 'token=' + UI_TOKEN : '';
}

function switchConversation(id) {
    if (parseInt(id) === CONV_ID) return;
    window.location.href = '/?c=' + id + _tokenParam('&');
}

async function loadConversations() {
    try {
        var url = '/api/conversations' + _tokenParam('?') +
            (SHOW_ARCHIVED ? '&archived_only=true' : '');
        var resp = await fetch(url, {
            headers: UI_TOKEN ? {'Authorization': 'Bearer ' + UI_TOKEN} : {}
        });
        var convs = await resp.json();
        var sel = document.getElementById('conv-select');
        sel.innerHTML = '';

        if (convs.length === 0) {
            var opt = document.createElement('option');
            opt.textContent = SHOW_ARCHIVED ? 'No archived chats' : 'No conversations';
            opt.disabled = true;
            sel.appendChild(opt);
            return;
        }

        for (var i = 0; i < convs.length; i++) {
            var c = convs[i];
            var opt = document.createElement('option');
            opt.value = c.id;
            var label = c.title || c.preview || ('Chat #' + c.id);

            // Track current conversation's archived status
            if (c.id === CONV_ID) {
                CURRENT_CONV_ARCHIVED = c.archived;
                opt.selected = true;
            }

            opt.textContent = label;
            sel.appendChild(opt);
        }

        // Update archive/unarchive button visibility
        updateArchiveButtons();
    } catch (err) {
        console.error('Failed to load conversations:', err);
    }
}

var SHOW_ARCHIVED = false;
var CURRENT_CONV_ARCHIVED = false;

async function archiveConversation(id) {
    try {
        var hdrs = {};
        if (UI_TOKEN) hdrs['Authorization'] = 'Bearer ' + UI_TOKEN;
        await fetch('/api/conversations/' + id + '/archive', {
            method: 'POST', headers: hdrs
        });
        await loadConversations();
        showToast('Conversation archived');
    } catch (err) {
        console.error('Failed to archive:', err);
        showToast('Failed to archive conversation', true);
    }
}

async function unarchiveConversation(id) {
    try {
        var hdrs = {};
        if (UI_TOKEN) hdrs['Authorization'] = 'Bearer ' + UI_TOKEN;
        await fetch('/api/conversations/' + id + '/unarchive', {
            method: 'POST', headers: hdrs
        });
        await loadConversations();
        showToast('Conversation unarchived');
    } catch (err) {
        console.error('Failed to unarchive:', err);
        showToast('Failed to unarchive conversation', true);
    }
}

function showArchiveModal() {
    var modal = document.getElementById('archive-modal');
    var text = document.getElementById('archive-modal-text');
    var sel = document.getElementById('conv-select');
    var currentTitle = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : 'this conversation';

    text.innerHTML = 'Archive "<strong>' + currentTitle + '</strong>"?<br><br>' +
        'This will hide it from your main list. You can view archived chats by switching to the "Archived" tab.';
    modal.style.display = 'flex';
}

function closeArchiveModal() {
    document.getElementById('archive-modal').style.display = 'none';
}

async function confirmArchive() {
    closeArchiveModal();
    await archiveConversation(CONV_ID);

    // Switch to another conversation after archiving
    var sel = document.getElementById('conv-select');
    if (sel.options.length > 0) {
        // Find first non-archived conversation
        for (var i = 0; i < sel.options.length; i++) {
            if (parseInt(sel.options[i].value) !== CONV_ID) {
                window.location.href = '/?c=' + sel.options[i].value + _tokenParam('&');
                return;
            }
        }
    }
    // No other conversations, create a new one
    window.location.href = '/new' + _tokenParam('?');
}

async function unarchiveCurrent() {
    await unarchiveConversation(CONV_ID);
    // Refresh to show updated state
    window.location.reload();
}

function setFilter(mode) {
    SHOW_ARCHIVED = (mode === 'archived');
    document.getElementById('tab-active').classList.toggle('active', !SHOW_ARCHIVED);
    document.getElementById('tab-archived').classList.toggle('active', SHOW_ARCHIVED);
    loadConversations();
}

function updateArchiveButtons() {
    var archiveBtn = document.getElementById('archive-btn');
    var unarchiveBtn = document.getElementById('unarchive-btn');

    if (CURRENT_CONV_ARCHIVED) {
        archiveBtn.style.display = 'none';
        unarchiveBtn.style.display = 'block';
    } else {
        archiveBtn.style.display = 'block';
        unarchiveBtn.style.display = 'none';
    }
}

function showToast(message, isError) {
    var toast = document.getElementById('toast');
    toast.textContent = message;
    toast.style.background = isError ? '#f92672' : '#a6e22e';
    toast.style.display = 'block';
    setTimeout(function() {
        toast.style.display = 'none';
    }, 3000);
}

// Auto-scroll after HTMX poll if user is near the bottom
var _wasAtBottom = true;
document.getElementById('chat-messages').addEventListener('scroll', function() {
    var el = this;
    _wasAtBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
});
document.body.addEventListener('htmx:afterSwap', function(evt) {
    if (evt.detail.target.id === 'chat-messages' && _wasAtBottom) {
        scrollToBottom();
    }
});

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
    // Ctrl/Cmd + D to archive/unarchive
    if ((e.ctrlKey || e.metaKey) && e.key === 'd') {
        e.preventDefault();
        if (CURRENT_CONV_ARCHIVED) {
            unarchiveCurrent();
        } else {
            showArchiveModal();
        }
    }
    // Ctrl/Cmd + Shift + A to toggle archived view
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'A') {
        e.preventDefault();
        setFilter(SHOW_ARCHIVED ? 'active' : 'archived');
    }
});

// Initial scroll, focus, and load conversations
document.getElementById('chat-input').focus();
scrollToBottom();
loadConversations();
