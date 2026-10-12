// Avisa quando algo precisa da pessoa sem ela estar olhando: o título da aba ganha um contador e,
// com os avisos do navegador ativados, uma notificação aparece se a aba estiver em segundo plano.
// Lê tudo do menu lateral, que o htmx atualiza sozinho a cada 10s.
(function () {
  const SETTLED = { succeeded: 'Execução concluída', failed: 'Execução falhou' };
  const STORE = 'sonny-notify';
  const baseTitle = document.title;
  // Execuções que terminaram enquanto a aba estava em segundo plano; zera quando a pessoa volta
  let unseen = 0;

  // O que a leitura anterior viu ({ runs: { id: situação }, pending: n }). Fica na sessão da aba
  // para a comparação continuar valendo quando a pessoa troca de página.
  function load() {
    try { return JSON.parse(sessionStorage.getItem(STORE)); } catch (e) { return null; }
  }

  function save(state) {
    try { sessionStorage.setItem(STORE, JSON.stringify(state)); } catch (e) {}
  }

  function notify(title, body, url) {
    if (!document.hidden || !('Notification' in window) || Notification.permission !== 'granted') return;
    // tag: abas diferentes avisando da mesma coisa viram uma notificação só
    const notification = new Notification(title, { body: body, tag: url });
    notification.onclick = function () {
      window.focus();
      window.location.href = url;
    };
  }

  function showTitle(pending) {
    const count = pending + unseen;
    document.title = (count ? '(' + count + ') ' : '') + baseTitle;
  }

  function update() {
    const nav = document.getElementById('sidebar-nav');
    if (!nav) return;
    const before = load();
    const pending = Number(nav.dataset.pendingApprovals || 0);
    const runs = {};
    nav.querySelectorAll('[data-run-id]').forEach(function (link) {
      const id = link.dataset.runId;
      const status = link.dataset.runStatus;
      runs[id] = status;
      // Só avisa de execução que esta aba já tinha visto em outra situação
      const previous = before && before.runs[id];
      if (previous && previous !== status && SETTLED[status]) {
        if (document.hidden) unseen += 1;
        notify(SETTLED[status], link.dataset.runTitle, link.href);
      }
    });
    if (before && pending > before.pending) {
      const text = pending === 1 ? 'Um pedido espera a sua decisão.' : pending + ' pedidos esperam a sua decisão.';
      notify('Aprovação pendente', text, nav.dataset.approvalsUrl);
    }
    save({ runs: runs, pending: pending });
    showTitle(pending);
  }

  document.addEventListener('DOMContentLoaded', update);
  document.addEventListener('htmx:afterSettle', update);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) {
      unseen = 0;
      update();
    }
  });
})();
