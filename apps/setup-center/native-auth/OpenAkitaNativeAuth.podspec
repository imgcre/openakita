Pod::Spec.new do |s|
  s.name = 'OpenAkitaNativeAuth'
  s.version = '1.0.0'
  s.summary = 'OpenAkita system browser authorization bridge'
  s.license = 'AGPL-3.0-only'
  s.homepage = 'https://github.com/openakita/openakita'
  s.author = 'OpenAkita'
  s.source = { :git => 'https://github.com/openakita/openakita.git' }
  s.source_files = 'ios/Sources/**/*.{swift,h,m}'
  s.ios.deployment_target = '15.0'
  s.dependency 'Capacitor'
  s.swift_version = '5.9'
end
