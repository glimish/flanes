import * as vscode from 'vscode';
import { FlaCli } from './cli';

/**
 * Status bar item showing the current lane and head hash.
 */
export class FlaStatusBar implements vscode.Disposable {
  private item: vscode.StatusBarItem;
  private cli: FlaCli;

  constructor(cli: FlaCli) {
    this.cli = cli;
    this.item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Left,
      100,
    );
    this.item.command = 'fla.status';
    this.item.tooltip = 'Click for Fla status';
    this.item.show();
  }

  async refresh(): Promise<void> {
    try {
      const status = await this.cli.getStatus();
      const lane = status.default_lane || 'main';
      const head = (status.head || 'none').substring(0, 8);
      this.item.text = `$(git-branch) ${lane} $(git-commit) ${head}`;
      this.item.color = undefined;
    } catch {
      this.item.text = '$(git-branch) fla (no repo)';
      this.item.color = new vscode.ThemeColor('statusBarItem.warningForeground');
    }
  }

  dispose(): void {
    this.item.dispose();
  }
}
