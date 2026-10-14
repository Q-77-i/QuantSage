/**
 * 幂等键（P3-E4b / L7）：**一次用户动作一个键**，重试复用同一个。
 *
 * 语义是「同一个动作」，不是「同一次调用」：
 *
 *   - 任务**还在跑**的时候再点一次（连点、手抖、页面重挂载）→ 复用同一个键，
 *     后端认出来是同一件事，回同一个 `task_id`，不会跑两遍；
 *   - 上一次已经落定之后的再点 → 那是**新的一次动作**（用户就是想再跑一遍），换键。
 *
 * 键由前端生成：只有发起动作的那一方知道「这两次点击是不是一回事」，服务端从请求体
 * 里看不出区别（同样的表单本来就该允许跑两遍）。
 */

/** 生成一个新键。`crypto.randomUUID` 要求安全上下文——本机 `127.0.0.1` 满足。 */
export function newIdempotencyKey(): string {
  return globalThis.crypto.randomUUID();
}

/** 一次动作的键。跟着「有没有任务在飞」在复用与换新之间切换。 */
export class SubmissionKey {
  private key: string | null = null;

  /** 取本次提交要用的键。`inFlight` = 上一个任务还没落定（那就还是同一次动作）。 */
  current(inFlight: boolean): string {
    if (!inFlight || this.key === null) {
      this.key = newIdempotencyKey();
    }
    return this.key;
  }
}
