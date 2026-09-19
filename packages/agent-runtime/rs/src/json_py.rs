//! `json.dumps` default spacing (`", "` / `": "`) so tool-message text matches Python.

use std::io::{self, Write};

use serde::Serialize;
use serde_json::ser::{Formatter, Serializer};

struct PythonFormatter;

impl Formatter for PythonFormatter {
    fn begin_array_value<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_key<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_value<W: ?Sized + Write>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }
}

pub fn dumps<T: Serialize>(value: &T) -> String {
    let mut buf = Vec::new();
    let mut ser = Serializer::with_formatter(&mut buf, PythonFormatter);
    value
        .serialize(&mut ser)
        .expect("tool-result payload is serializable");
    String::from_utf8(buf).expect("serde_json writes utf-8")
}
