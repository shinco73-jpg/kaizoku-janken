# Renderで公開する（おすすめ・かんたん）

Renderは無料で使えるクラウドです。**httpsのURLが自動でつく**ので、
証明書やドメイン、nginxの設定は要りません。今のファイルがそのまま動きます。

流れは「①ファイルをGitHubに置く → ②RenderをGitHubにつなぐ」の2ステップです。
どちらも画面のクリックだけででき、コマンドは不要です。

---

## 用意するもの

- GitHub のアカウント（無料）… https://github.com
- Render のアカウント（無料）… https://render.com （GitHubアカウントでログインできます）

置くファイルは5つ。ぜんぶ同じフォルダに入れておきます。

- `server.py`
- `index.html`
- `requirements.txt`
- `render.yaml`
- （README類は任意）

---

## ① GitHubにファイルを置く（ブラウザだけでOK）

1. GitHubにログインし、右上の「＋」→「New repository」。
2. 名前（例：`kaizoku-janken`）を入れて「Create repository」。
   ※ Public でも Private でもどちらでも動きます。
3. できたページの「uploading an existing file」を押す。
4. `server.py` `index.html` `requirements.txt` `render.yaml` を
   **ドラッグ＆ドロップ**して、下の「Commit changes」を押す。

これでファイルがGitHubに乗りました。

---

## ② RenderをGitHubにつなぐ

1. Renderにログイン → 右上「New +」→ **「Blueprint」** を選ぶ。
   （`render.yaml` を置いたので、設定はほぼ自動で読み込まれます）
2. さっき作ったGitHubのリポジトリを選ぶ。初回は「GitHubと連携」を求められるので許可する。
3. 内容を確認して「Apply」/「Deploy」。
4. しばらく待つと、`https://kaizoku-janken-xxxx.onrender.com` のような
   **公開URL**が発行されます。これを開けば遊べます。

> もし「Blueprint」が見つからない・うまくいかない場合は、代わりに
> 「New +」→「Web Service」→ リポジトリを選び、次を手入力してもOKです。
> - Language: **Python**
> - Build Command: `pip install -r requirements.txt`
> - Start Command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
> - Instance Type: **Free**

---

## 遊び方

公開URLを友達に送るだけ。

1. ひとりが「新しい部屋を作る」→ 4文字の部屋コードが出る
2. コードを友達に伝える
3. 友達が同じURLで「部屋コードで参加」にコードを入れる
4. 2人そろって「準備OK!!」で対戦スタート

---

## Renderの無料枠で知っておくこと

- **しばらく誰も開かないとサーバーが眠ります**（約15分）。
  次に開いたとき、起きるのに30〜60秒くらいかかります（画面が出るまで少し待つ）。
  一度起きれば、その後はサクサク動きます。
- データはメモリ上だけなので、眠って起きると「強さ」などはリセットされます
  （記録を残したい場合は、あとでデータベースを足せます）。
- あとからファイルを直したいときは、GitHubのファイルを更新すれば
  Renderが自動で入れ替えてくれます。

うまくいかないところがあれば、Renderの画面に出るログ（赤い文字など）を
そのままコピーして貼ってください。一緒に直します。
