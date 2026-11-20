FROM hibuz/zsh

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates jq python3-venv util-linux \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN git config --system init.defaultBranch main

# named volume 마운트 지점을 미리 만들어 두면 새 볼륨이 ubuntu 소유로 초기화된다
RUN install -d -o ubuntu -g ubuntu -m 0700 /home/ubuntu/.hermes \
 && install -d -o ubuntu -g ubuntu -m 0755 /home/ubuntu/.claude /home/ubuntu/.codex \
 && install -d -o ubuntu -g ubuntu -m 0755 /workspaces

RUN printf '\n%s\n' '[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || export PATH="$HOME/.local/bin:$PATH"' >> /etc/zsh/zshenv

USER ubuntu
