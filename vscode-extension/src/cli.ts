import * as vscode from 'vscode';
import { execFile } from 'child_process';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

/**
 * Wrapper around the `vex` CLI. All interaction with Vex goes through
 * the CLI with `--json` output for machine-readable results.
 */
export class VexCli {
  private get vexPath(): string {
    return vscode.workspace.getConfiguration('vex').get<string>('path', 'vex');
  }

  private get cwd(): string | undefined {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  }

  /** Run a vex CLI command and return parsed JSON output. */
  async run(args: string[]): Promise<any> {
    const cwd = this.cwd;
    if (!cwd) {
      throw new Error('No workspace folder open');
    }

    try {
      const { stdout } = await execFileAsync(this.vexPath, [...args, '--json'], {
        cwd,
        timeout: 30000,
      });
      return JSON.parse(stdout);
    } catch (err: any) {
      // Try to parse JSON error from stderr
      if (err.stderr) {
        try {
          const parsed = JSON.parse(err.stderr);
          throw new Error(parsed.error || err.stderr);
        } catch {
          throw new Error(err.stderr || err.message);
        }
      }
      throw err;
    }
  }

  /** Get repository status. */
  async getStatus(): Promise<any> {
    return this.run(['status']);
  }

  /** Get lane list. */
  async getLanes(): Promise<any> {
    return this.run(['lanes']);
  }

  /** Get history for a lane. */
  async getHistory(lane?: string, limit = 50): Promise<any> {
    const args = ['history', '--limit', String(limit)];
    if (lane) args.push('--lane', lane);
    return this.run(args);
  }

  // ── Interactive commands ──

  async showStatus(): Promise<void> {
    try {
      const status = await this.getStatus();
      const msg = `Lane: ${status.default_lane} | Head: ${(status.head || 'none').substring(0, 12)}`;
      vscode.window.showInformationMessage(msg);
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex status: ${err.message}`);
    }
  }

  async snapshot(): Promise<void> {
    try {
      const result = await this.run(['snapshot']);
      vscode.window.showInformationMessage(
        `Snapshot created: ${result.state_id?.substring(0, 12)}`
      );
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex snapshot: ${err.message}`);
    }
  }

  async commit(): Promise<void> {
    const message = await vscode.window.showInputBox({
      prompt: 'Commit message',
      placeHolder: 'Describe the changes...',
    });
    if (!message) return;

    try {
      const result = await this.run([
        'commit', '-m', message, '--auto-accept',
      ]);
      vscode.window.showInformationMessage(
        `Committed: ${result.transition_id?.substring(0, 12)}`
      );
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex commit: ${err.message}`);
    }
  }

  async showHistory(): Promise<void> {
    try {
      const history = await this.getHistory(undefined, 20);
      const items = history.map((t: any) => ({
        label: `${t.status} ${(t.id || '').substring(0, 8)}`,
        description: t.prompt?.substring(0, 60),
        detail: `Lane: ${t.lane}`,
      }));
      vscode.window.showQuickPick(items, { placeHolder: 'Recent transitions' });
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex history: ${err.message}`);
    }
  }

  async showLanes(): Promise<void> {
    try {
      const lanes = await this.getLanes();
      const items = Object.entries(lanes).map(([name, head]) => ({
        label: name,
        description: String(head || 'empty').substring(0, 12),
      }));
      vscode.window.showQuickPick(items, { placeHolder: 'Lanes' });
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex lanes: ${err.message}`);
    }
  }

  async createLane(): Promise<void> {
    const name = await vscode.window.showInputBox({
      prompt: 'Lane name',
      placeHolder: 'feature-xyz',
    });
    if (!name) return;

    try {
      await this.run(['lane', 'create', name]);
      vscode.window.showInformationMessage(`Lane '${name}' created`);
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex create lane: ${err.message}`);
    }
  }

  async switchLane(): Promise<void> {
    try {
      const lanes = await this.getLanes();
      const picked = await vscode.window.showQuickPick(
        Object.keys(lanes),
        { placeHolder: 'Select lane to switch to' },
      );
      if (picked) {
        vscode.window.showInformationMessage(`Selected lane: ${picked}`);
      }
    } catch (err: any) {
      vscode.window.showErrorMessage(`Vex switch lane: ${err.message}`);
    }
  }

  async showDiff(): Promise<void> {
    vscode.window.showInformationMessage(
      'Diff view: use Vex History panel to select transitions'
    );
  }
}
