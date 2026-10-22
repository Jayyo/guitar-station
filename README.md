# 기타 스테이션 (guitar-station)

일렉기타/베이스를 PreSonus AudioBox USB 96에 물려 데스크톱 출력으로 모니터링하는 GTK4 앱.
매번 `pw-loopback` + `pw-link`를 손으로 조립하지 않기 위해 만들었다.

```
guitar-station          # GUI (GNOME 앱 목록 "기타 스테이션")
guitar-monitor on       # 헤드리스 켜기
guitar-monitor off      # 끄기
guitar-monitor level 5  # 5초 재서 게인 판정
guitar-monitor tune 15  # 15초 동안 튜너 (터미널 출력)
guitar-monitor status
```

## 앱이 대신 해주는 것

- **Input 1 → 좌우 양쪽 복제.** Input 1은 왼쪽 채널로만 들어와서 그냥 루프백하면 한쪽에서만 들린다.
  `pw-loopback --capture-props='audio.position=[MONO]'`는 실측에서 안 먹었고, `pw-link`로
  `capture_FL`을 `input_FR`에도 손으로 연결하는 것만 동작했다.
- **재연결 복구.** USB를 다시 꽂으면 노드가 새로 생기면서 그 수동 배선이 기본값으로 되돌아간다.
  앱은 켤 때마다 다시 건다.
- **출력 추종.** 기본 sink는 수시로 바뀐다(인터페이스를 꽂으면 자동 전환, 헤드셋을 켜면 또 전환).
  출력을 "기본 출력 따라가기"로 두면 루프백이 알아서 따라간다. 특정 장치로 고정도 가능.
- **실시간 레벨 미터.** 모니터링이 꺼져 있어도 동작하므로, 소리를 어디로 보내기 전에 Gain 노브를
  맞출 수 있다. 목표는 peak -14 ~ -5 dBFS, 클리핑 0.
- **튜너.** 현 하나를 울리면 가장 가까운 개방현(베이스 4/5현·기타 6현 프리셋, 또는 크로매틱)과
  센트 오차를 바늘로 보여준다. ±3센트면 초록. 검출은 `guitarstation/tuner.py`의 YIN(FFT 차분함수,
  8192 샘플 창)이며 5현 베이스 저음 B0(30.9 Hz)까지 잡는다. 합성음 110케이스 오차 <1센트,
  실제 베이스 스템 지속음 ±2센트, 청크당 0.8 ms. 기준음 A4 조절 가능(415~466).
- **무신호 자동 정지(선택).** 켜둔 채 잊으면 기타 픽업이 줍는 험이 헤드폰에 계속 섞인다.

## 구조

- `guitarstation/core.py` — PipeWire 조회·루프백 제어·레벨 측정. GUI 의존성 없음:
  `python3 -c "from guitarstation import core; print(core.status())"`
- `guitarstation/tuner.py` — YIN 피치 검출 + 음이름/개방현 계산. numpy만 쓴다. `tests/test_tuner.py`로 검증.
- `guitarstation/app.py` — GTK4/libadwaita 창. core를 렌더링만 한다.
- `bin/guitar-station` (GUI) / `bin/guitar-monitor` (CLI) — 둘 다 같은 core를 쓴다.

설정: `~/.config/guitar-station/config.json`

## 함정 메모

- **`Adw.PreferencesGroup.add()`에 row가 아닌 위젯을 넣으면 그 그룹의 row들 *아래*에 그려진다.**
  튜너 표시부(글자·바늘)를 프리셋 row 위에 두려면 그룹을 둘로 나눠야 했다.
- **헤드리스 GUI 테스트는 application_id를 바꿔서 띄울 것.** 같은 id로 띄우면 D-Bus로 이미 떠 있는
  사용자 창을 activate만 하고 즉시 종료해 콜백이 하나도 안 돈다(Xvfb 안에서도 세션 버스는 공유).
  스크린샷은 `Gtk.WidgetPaintable` → `renderer.render_texture()` → `save_to_png`.

- **PipeWire 노드 이름을 저장하면 안 된다.** 같은 G733이 `iec958-stereo` ↔ `analog-stereo`로 바뀐다.
  출력 장치는 프로파일 접미사를 뗀 접두사를 지문으로 쓴다.
- **인터페이스 입력 노드는 매번 다시 조회한다.** 재연결하면 이름이 달라진다.
- **AudioBox는 ALSA 컨트롤이 없다** (`amixer -c 3`이 빈 출력). Gain·Mixer·Main·Phones는 전부
  하드웨어 노브라 소프트웨어로 못 만진다. 앱이 게인을 "조절"하지 못하고 "판정"만 하는 이유.
- **`arecord -D hw:3,0`은 PipeWire 점유 때문에 busy.** 측정은 `pw-cat --record --target <노드> -`로.
  `pw-record --raw -`는 stdout으로 아무것도 안 뱉는다(실측 0바이트).
