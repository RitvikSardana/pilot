import interUrl from '@inter-font?url'

/** Add Inter to the page. A font declared inside a shadow root does not apply, so it must live in the document. */
export const loadInter = (): void => {
  const style = document.createElement('style')
  style.textContent = `@font-face {
  font-family: 'InterVar';
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: url(${JSON.stringify(interUrl)}) format('woff2');
}`
  document.head.append(style)
}
