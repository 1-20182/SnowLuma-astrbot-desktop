/**
 * 预加载脚本：只暴露最少的东西给页面。
 *   window.slabDesktop.isDesktop      → 是否跑在桌面壳里
 *   window.slabDesktop.quit()         → 让外壳退出（同时会杀掉后端进程树）
 *   window.slabDesktop.pickDirectory()→ 原生「选择文件夹」对话框，返回绝对路径
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('slabDesktop', {
  isDesktop: true,
  version: process.versions.electron,
  quit: () => ipcRenderer.invoke('app:quit'),
  openPanel: (url, creds) => ipcRenderer.invoke('panel:open', { url, creds }),
  winAction: (action) => ipcRenderer.invoke('win:action', action),
  pickZip: () => ipcRenderer.invoke('dialog:pick-zip'),
  pickDirectory: (title, defaultPath) =>
    ipcRenderer.invoke('dialog:pick-dir', { title, defaultPath }),
});
