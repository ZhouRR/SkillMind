/* CSS/React の読込前に保存テーマを反映し、再読込時の反対色の閃光を避ける。 */
try {
  document.documentElement.dataset.theme = localStorage.getItem('skillmind.theme') === 'light' ? 'light' : 'dark'
} catch {
  document.documentElement.dataset.theme = 'dark'
}
