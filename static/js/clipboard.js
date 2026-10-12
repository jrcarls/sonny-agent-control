// copyText(texto): copia para a área de transferência e devolve uma Promise.
// navigator.clipboard só existe em página segura (https ou localhost); fora disso usa o comando antigo do navegador.
window.copyText = function (text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise(function (resolve, reject) {
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand('copy');
    field.remove();
    copied ? resolve() : reject(new Error('copy'));
  });
};
