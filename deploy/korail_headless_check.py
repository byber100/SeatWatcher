from __future__ import annotations

import json
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "control"
DIAGNOSTICS = RUNTIME / "diagnostics"
RESULT_FILE = DIAGNOSTICS / "latest_headless.json"
SCREENSHOT_FILE = DIAGNOSTICS / "latest_headless.png"


def load_target() -> dict:
    local = ROOT / "watch_targets.local.json"
    public = ROOT / "watch_targets.json"
    source = local if local.exists() else public
    config = json.loads(source.read_text(encoding="utf-8"))
    targets = config.get("rail_targets") or []
    if not targets:
        raise RuntimeError("rail_targets is empty")
    return dict(targets[0])


def save(payload: dict) -> None:
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    from playwright.sync_api import sync_playwright

    target = load_target()
    departure = str(target["departure"])
    arrival = str(target["arrival"])
    date = str(target["date"])
    start = str(target["start"])
    day = str(int(date[6:8]))
    hour = f"{int(start[:2]):02d}"

    state: dict = {
        "stage": "start",
        "target": {
            "departure": departure,
            "arrival": arrival,
            "date": date,
            "start": start,
            "end": str(target.get("end", "")),
        },
        "responses": [],
        "fails": [],
    }
    save(state)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={"width": 1440, "height": 1200},
            )
            page.set_default_timeout(15000)

            def on_response(response) -> None:
                if response.request.resource_type not in ("xhr", "fetch"):
                    return
                url = response.url
                if "korail.com" not in url and "letskorail.com" not in url:
                    return
                item = {
                    "status": response.status,
                    "method": response.request.method,
                    "url": url,
                }
                if "/web_s/" in url or "ScheduleView" in url:
                    try:
                        item["body"] = response.text()[:4000]
                    except Exception as exc:
                        item["body_error"] = str(exc)
                state["responses"].append(item)

            page.on("response", on_response)
            page.on(
                "requestfailed",
                lambda request: state["fails"].append(
                    {"url": request.url, "failure": str(request.failure)}
                ),
            )

            state["stage"] = "navigate"
            save(state)
            page.goto(
                "https://www.korail.com/ticket/search/general",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.locator('input[name="txtGoStart"]').wait_for(
                state="visible",
                timeout=45000,
            )

            if not page.evaluate(
                "() => { const a=document.querySelector('.start a.btn_pop');"
                "if(!a)return false;a.click();return true }"
            ):
                raise RuntimeError("start popup missing")
            page.wait_for_timeout(400)
            selected = page.evaluate(
                """(name) => {
                    const a=[...document.querySelectorAll('a')].find(
                      x=>(x.innerText||'').trim()===name &&
                      x.getAttribute('aria-disabled')!=='true'
                    );
                    if(!a)return false;a.click();return true;
                }""",
                departure,
            )
            if not selected:
                raise RuntimeError(f"departure station missing: {departure}")
            page.wait_for_timeout(400)

            if not page.evaluate(
                "() => { const a=document.querySelector('.end a.btn_pop');"
                "if(!a)return false;a.click();return true }"
            ):
                raise RuntimeError("end popup missing")
            page.wait_for_timeout(400)
            selected = page.evaluate(
                """(name) => {
                    const a=[...document.querySelectorAll('a')].find(
                      x=>(x.innerText||'').trim()===name &&
                      x.getAttribute('aria-disabled')!=='true'
                    );
                    if(!a)return false;a.click();return true;
                }""",
                arrival,
            )
            if not selected:
                raise RuntimeError(f"arrival station missing: {arrival}")
            page.wait_for_timeout(400)

            if not page.evaluate(
                "() => { const a=document.querySelector('a.btn_d-day');"
                "if(!a)return false;a.click();return true }"
            ):
                raise RuntimeError("date popup missing")
            page.wait_for_timeout(500)

            selected_day = page.evaluate(
                """(day) => {
                    const a=[...document.querySelectorAll('a')].find(x=>{
                      const td=x.closest('td');
                      return (x.innerText||'').trim()===day &&
                        x.getAttribute('aria-disabled')!=='true' &&
                        !(td && td.classList.contains('disabled'));
                    });
                    if(!a)return false;a.click();return true;
                }""",
                day,
            )
            if not selected_day:
                raise RuntimeError(f"day missing in current date picker: {day}")
            page.wait_for_timeout(300)

            requested_hour = int(hour)
            observed_hours: set[int] = set()
            hour_state = {"selected": None, "enabled": [], "next_clicked": False}
            for _hour_page in range(8):
                hour_state = page.evaluate(
                    """(requested) => {
                        const root =
                          document.querySelector('.timeSelect') ||
                          document.querySelector('.time_select');
                        if (!root) {
                            return {selected:null, enabled:[], next_clicked:false, debug:'root_missing'};
                        }
                        const candidates=[...root.querySelectorAll('a,button')]
                          .map(node => ({
                            node,
                            text:(node.innerText||'').trim().replace('시',''),
                            disabled:node.getAttribute('aria-disabled')
                          }))
                          .filter(item => /^([01]?[0-9]|2[0-3])$/.test(item.text));
                        const enabled=candidates
                          .filter(item => item.disabled!=='true')
                          .map(item => parseInt(item.text,10));
                        const exact=candidates.find(
                          item => parseInt(item.text,10)===requested &&
                                  item.disabled!=='true'
                        );
                        if (exact) {
                            exact.node.click();
                            return {selected:requested, enabled:[...new Set(enabled)], next_clicked:false};
                        }

                        const scope =
                          root.closest('.popup,.pop_wrap,.layer,.modal,.date_layer,.calendar') ||
                          root.parentElement?.parentElement ||
                          root.parentElement ||
                          root;
                        const navs=[...scope.querySelectorAll('a,button')]
                          .filter(node => {
                            const text=(node.innerText||'').trim().replace('시','');
                            if (/^([01]?[0-9]|2[0-3])$/.test(text)) return false;
                            const meta=[
                              node.innerText||'',
                              node.getAttribute('aria-label')||'',
                              node.getAttribute('title')||'',
                              typeof node.className==='string' ? node.className : ''
                            ].join(' ').toLowerCase();
                            return /다음|next|right|arr[_-]?r|arrow[_-]?right|btn[_-]?next|swiper-button-next/.test(meta) &&
                              node.getAttribute('aria-disabled')!=='true';
                          });
                        if (navs.length) {
                            navs[0].click();
                            return {selected:null, enabled:[...new Set(enabled)], next_clicked:true};
                        }
                        return {
                          selected:null,
                          enabled:[...new Set(enabled)],
                          next_clicked:false,
                          debug:(scope.outerHTML||'').slice(0,4000)
                        };
                    }""",
                    requested_hour,
                )
                observed_hours.update(int(value) for value in (hour_state.get("enabled") or []))
                if hour_state.get("selected") == requested_hour:
                    break
                if not hour_state.get("next_clicked"):
                    break
                page.wait_for_timeout(250)

            state["requested_hour"] = requested_hour
            state["enabled_hours"] = sorted(observed_hours)
            state["selected_hour"] = hour_state.get("selected")
            state["hour_exact"] = state["selected_hour"] == requested_hour
            if not state["hour_exact"]:
                state["hour_debug"] = hour_state.get("debug")
                save(state)
                raise RuntimeError(
                    f"requested hour unavailable in KORAIL web UI: "
                    f"requested={requested_hour}, enabled={state['enabled_hours']}"
                )
            selected_hour_value = requested_hour
            page.wait_for_timeout(300)

            applied = page.evaluate(
                """() => {
                    const b=[...document.querySelectorAll('button')].find(
                      x=>(x.innerText||'').trim()==='적용' &&
                      x.classList.contains('btn_bn-blue')
                    );
                    if(!b)return false;b.click();return true;
                }"""
            )
            if not applied:
                raise RuntimeError("date apply button missing")
            page.wait_for_timeout(500)

            state["form"] = {
                "start": page.locator('input[name="txtGoStart"]').input_value(),
                "end": page.locator('input[name="txtGoEnd"]').input_value(),
                "date": page.locator("#startDate").input_value(),
            }
            expected_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            state["form_verified"] = (
                state["form"]["start"] == departure
                and state["form"]["end"] == arrival
                and expected_date in state["form"]["date"]
                and f" {int(selected_hour_value):02d}:" in state["form"]["date"]
            )
            if not state["form_verified"]:
                raise RuntimeError(f"form mismatch: {state['form']}")

            state["stage"] = "search"
            save(state)
            mark = len(state["responses"])
            clicked = page.evaluate(
                "() => { const b=document.querySelector('button.btn_lookup');"
                "if(!b)return false;b.click();return true }"
            )
            if not clicked:
                raise RuntimeError("search button missing")
            try:
                page.wait_for_url("**/ticket/search/list", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(12000)

            state["result_url"] = page.url
            state["result_text"] = page.locator("body").inner_text()[:16000]
            state["responses_after_search"] = state["responses"][mark:]
            web_schedule = [
                item
                for item in state["responses_after_search"]
                if "/web_s/" in item.get("url", "")
            ]
            state["schedule_http_statuses"] = [
                item.get("status") for item in web_schedule
            ]
            state["ok"] = bool(
                web_schedule
                and any(item.get("status") == 200 for item in web_schedule)
                and "해당 스케줄에 운행하는 열차가 없습니다." not in state["result_text"]
            )
            state["stage"] = "done"
            page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
            state["screenshot"] = str(SCREENSHOT_FILE)
            save(state)
            browser.close()
    except Exception as exc:
        state["stage"] = "error"
        state["ok"] = False
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["trace"] = traceback.format_exc()[-5000:]
        try:
            save(state)
        except Exception:
            pass

    print(json.dumps(state, ensure_ascii=False))
    return 0 if state.get("stage") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
