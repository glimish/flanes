import * as vscode from 'vscode';
import { VexCli } from './cli';
import { VexStatusBar } from './statusbar';
import { VexHistoryProvider } from './history';

let statusBar: VexStatusBar | undefined;
let refreshInterval: ReturnType<typeof setInterval> | undefined;

export function activate(context: vscode.ExtensionContext) {
  const cli = new VexCli();

  // Status bar
  statusBar = new VexStatusBar(cli);
  context.subscriptions.push(statusBar);

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand('vex.status', () => cli.showStatus()),
    vscode.commands.registerCommand('vex.snapshot', () => cli.snapshot()),
    vscode.commands.registerCommand('vex.commit', () => cli.commit()),
    vscode.commands.registerCommand('vex.history', () => cli.showHistory()),
    vscode.commands.registerCommand('vex.lanes', () => cli.showLanes()),
    vscode.commands.registerCommand('vex.createLane', () => cli.createLane()),
    vscode.commands.registerCommand('vex.switchLane', () => cli.switchLane()),
    vscode.commands.registerCommand('vex.diff', () => cli.showDiff()),
  );

  // History tree view
  const historyProvider = new VexHistoryProvider(cli);
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('vexHistory', historyProvider),
  );

  // Auto-refresh
  const config = vscode.workspace.getConfiguration('vex');
  if (config.get<boolean>('autoRefresh', true)) {
    const interval = config.get<number>('refreshInterval', 5000);
    refreshInterval = setInterval(() => statusBar?.refresh(), interval);
  }

  // Initial refresh
  statusBar.refresh();
}

export function deactivate() {
  if (refreshInterval) clearInterval(refreshInterval);
  statusBar?.dispose();
}
